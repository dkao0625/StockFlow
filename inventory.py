"""
Inventory pages and the employee -> manager request workflow.

Workflow:
  1. An employee submits a request -> status "Pending". A request is either
       - a stock adjustment (Damaged/Missing report, or a bulk "Adjust stock"
         increase/decrease), or
       - an archive request (bulk "Delete" from the inventory page).
  2. Manager approves -> the change is applied (stock moves, or the product
     is archived) and recorded.
     Manager rejects  -> nothing changes.
  3. Employee sees the updated status on their dashboard.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import employee_required, login_required, manager_required
from models import (
    DIRECTION_DECREASE,
    PRODUCT_CATEGORIES,
    REQUEST_ADJUSTMENT,
    REQUEST_REASONS,
    STOCK_IN,
    STOCK_LOW,
    STOCK_OUT,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ActivityLog,
    InventoryRequest,
    Product,
    db,
    log_activity,
)
from stock import StockError, archive_product, change_stock, lock_products

inventory_bp = Blueprint("inventory", __name__)

MAX_REQUEST_QUANTITY = 100_000
MAX_NOTE_LENGTH = 500
STOCK_STATUSES = [STOCK_IN, STOCK_LOW, STOCK_OUT]


def active_products():
    """Products that are not archived, by name."""
    return Product.query.filter_by(is_archived=False).order_by(Product.name).all()


def inventory_stats(products):
    """Numbers for the summary cards at the top of the dashboards."""
    return {
        "total_products": len(products),
        "total_units": sum(p.quantity for p in products),
        "low_stock": sum(1 for p in products if p.stock_status == STOCK_LOW),
        "out_of_stock": sum(1 for p in products if p.stock_status == STOCK_OUT),
    }


def parse_positive_quantity(raw_value):
    """Turn form text into a whole number from 1 to MAX_REQUEST_QUANTITY, or None."""
    try:
        quantity = int(raw_value)
    except (TypeError, ValueError):
        return None
    if quantity < 1 or quantity > MAX_REQUEST_QUANTITY:
        return None
    return quantity


# ---------------------------------------------------------------------------
# Pages any logged-in user can see
# ---------------------------------------------------------------------------

@inventory_bp.route("/")
def index():
    if g.user is None:
        return redirect(url_for("auth.login"))
    if g.user.is_manager:
        return redirect(url_for("inventory.manager_dashboard"))
    return redirect(url_for("inventory.employee_dashboard"))


@inventory_bp.route("/inventory")
@login_required
def inventory_list():
    # Search and filters come from the URL, e.g. /inventory?q=tape&category=Other
    filters = {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", ""),
        "status": request.args.get("status", ""),
        "show": request.args.get("show", "active"),  # "active" or "archived"
    }
    showing_archived = filters["show"] == "archived"

    query = Product.query.filter_by(is_archived=showing_archived)
    if filters["q"]:
        # ilike = case-insensitive LIKE. SQLAlchemy sends the search text as a
        # parameter, never pasted into the SQL, so this is injection-safe.
        pattern = f"%{filters['q']}%"
        query = query.filter(
            db.or_(Product.name.ilike(pattern), Product.sku.ilike(pattern), Product.location.ilike(pattern))
        )
    if filters["category"] in PRODUCT_CATEGORIES:
        query = query.filter_by(category=filters["category"])
    products = query.order_by(Product.name).all()

    # Status isn't stored (it's calculated), so it's filtered in Python.
    if filters["status"] in STOCK_STATUSES:
        products = [p for p in products if p.stock_status == filters["status"]]

    return render_template(
        "inventory.html",
        products=products,
        stats=inventory_stats(active_products()),
        filters=filters,
        showing_archived=showing_archived,
        is_filtered=bool(filters["q"] or filters["category"] or filters["status"]),
        categories=PRODUCT_CATEGORIES,
        statuses=STOCK_STATUSES,
    )


# ---------------------------------------------------------------------------
# Employee pages
# ---------------------------------------------------------------------------

@inventory_bp.route("/employee")
@employee_required
def employee_dashboard():
    products = active_products()
    # Only THIS employee's requests -- filtering by employee_id on the
    # server is what keeps other employees' requests private.
    my_requests = (
        InventoryRequest.query.filter_by(employee_id=g.user.id)
        .order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .all()
    )
    return render_template(
        "employee.html",
        products=products,
        # Pre-select a product when coming from its detail page (?product_id=5)
        selected_product_id=request.args.get("product_id", type=int),
        my_requests=my_requests,
        reasons=REQUEST_REASONS,
        stats=inventory_stats(products),
    )


@inventory_bp.route("/requests", methods=["POST"])
@employee_required
def submit_request():
    product_id = request.form.get("product_id", type=int)
    reason = request.form.get("reason", "")
    quantity = parse_positive_quantity(request.form.get("quantity"))
    notes = request.form.get("notes", "").strip()

    product = db.session.get(Product, product_id) if product_id else None

    # Validate everything on the server.
    error = None
    if product is None or product.is_archived:
        error = "Please choose a valid product."
    elif reason not in REQUEST_REASONS:
        error = "Please choose Damaged or Missing."
    elif quantity is None:
        error = f"Quantity must be a whole number between 1 and {MAX_REQUEST_QUANTITY:,}."
    elif quantity > product.quantity:
        error = f"Only {product.quantity} unit(s) of {product.name} are in stock."
    elif not notes:
        error = "Please add a short explanation."
    elif len(notes) > MAX_NOTE_LENGTH:
        error = f"Explanation must be {MAX_NOTE_LENGTH} characters or fewer."

    if error:
        flash(error, "error")
        return redirect(url_for("inventory.employee_dashboard"))

    new_request = InventoryRequest(
        product_id=product.id,
        employee_id=g.user.id,
        request_type=REQUEST_ADJUSTMENT,
        direction=DIRECTION_DECREASE,  # damaged/missing items leave stock
        quantity=quantity,
        reason=reason,
        notes=notes,
        status=STATUS_PENDING,
    )
    db.session.add(new_request)
    db.session.flush()  # gives new_request an id for the log message
    log_activity(
        g.user.id,
        f"Submitted request #{new_request.id}: {reason} x{quantity} of {product.sku}",
        product.id,
    )
    db.session.commit()

    flash("Report submitted. A manager will review it.", "success")
    return redirect(url_for("inventory.employee_dashboard"))


# ---------------------------------------------------------------------------
# Manager pages
# ---------------------------------------------------------------------------

@inventory_bp.route("/manager")
@manager_required
def manager_dashboard():
    pending_requests = (
        InventoryRequest.query.filter_by(status=STATUS_PENDING)
        .order_by(InventoryRequest.created_at, InventoryRequest.id)
        .all()
    )
    request_history = (
        InventoryRequest.query.filter(InventoryRequest.status != STATUS_PENDING)
        .order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "manager.html",
        pending_requests=pending_requests,
        request_history=request_history,
        stats=inventory_stats(active_products()),
    )


@inventory_bp.route("/activity")
@manager_required
def activity():
    """Inventory activity history: who did what, newest first."""
    entries = ActivityLog.query.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(200).all()
    return render_template("activity.html", entries=entries)


# ---------------------------------------------------------------------------
# Approving and rejecting requests
# ---------------------------------------------------------------------------

def lock_requests(request_ids):
    """
    Load requests with row locks (SELECT ... FOR UPDATE), ordered by id.

    While this transaction holds the lock, any other transaction that tries
    to lock the same rows must WAIT until we commit or roll back. So if two
    managers click "Approve" at the same moment, the second one waits, then
    sees the status is no longer Pending, and stops.
    """
    return db.session.execute(
        db.select(InventoryRequest)
        .where(InventoryRequest.id.in_(request_ids))
        .order_by(InventoryRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()


def apply_decision(inventory_request, product, approve, manager_note):
    """
    Approve or reject ONE locked, pending request (inside the caller's
    transaction). Raises StockError if approving would break a rule.
    """
    if approve:
        if inventory_request.is_archive:
            archive_product(product, g.user.id, f"approved request #{inventory_request.id}")
        else:
            # Stock calculation: +quantity for increases, -quantity for decreases.
            change_stock(
                product,
                inventory_request.signed_quantity,
                g.user.id,
                note=f"{inventory_request.reason} (request #{inventory_request.id})",
                request_id=inventory_request.id,
            )
        inventory_request.status = STATUS_APPROVED
    else:
        # Rejecting changes ONLY the request. Product stock is untouched.
        inventory_request.status = STATUS_REJECTED

    inventory_request.manager_id = g.user.id
    inventory_request.manager_note = manager_note or None
    log_activity(
        g.user.id,
        f"{inventory_request.status} request #{inventory_request.id}: {inventory_request.summary}",
        product.id,
    )


def decide_requests(request_ids, approve):
    """
    Approve or reject several requests as ONE transaction: if any of them
    can't be approved, nothing is saved at all. Returns a redirect.
    """
    manager_note = request.form.get("manager_note", "").strip()
    error = None
    if len(manager_note) > MAX_NOTE_LENGTH:
        error = f"Note must be {MAX_NOTE_LENGTH} characters or fewer."
    elif not approve and not manager_note:
        error = "Please add a note explaining why the request is rejected."
    if error:
        flash(error, "error")
        return redirect(url_for("inventory.manager_dashboard"))

    # ---- Start of the database transaction --------------------------------
    # Everything until commit() happens as one unit: either ALL changes are
    # saved (statuses, stock, movements, logs) or NONE are (rollback).
    try:
        locked_requests = lock_requests(request_ids)
        if len(locked_requests) != len(request_ids):
            db.session.rollback()
            abort(404)
        already_done = [r.id for r in locked_requests if r.status != STATUS_PENDING]
        if already_done:
            db.session.rollback()
            flash(f"Request #{already_done[0]} was already processed. Nothing was changed.", "warning")
            return redirect(url_for("inventory.manager_dashboard"))

        # Lock every affected product too (request rows first, then product
        # rows -- the same order everywhere, which avoids deadlocks).
        products = lock_products({r.product_id for r in locked_requests})
        for inventory_request in locked_requests:
            apply_decision(inventory_request, products[inventory_request.product_id], approve, manager_note)

        db.session.commit()  # saves everything and releases the row locks
    except StockError as problem:
        db.session.rollback()
        if len(request_ids) == 1:
            prefix = f"Cannot approve request #{request_ids[0]}: "
        else:
            prefix = "Nothing was approved. "
        flash(f"{prefix}{problem} Reject that request or adjust stock first.", "error")
        return redirect(url_for("inventory.manager_dashboard"))
    except Exception:
        # Any unexpected error: undo all partial changes, then re-raise so
        # Flask shows an error page and the problem is logged.
        db.session.rollback()
        raise
    # ---- End of the database transaction ----------------------------------

    verb = "approved" if approve else "rejected"
    if len(request_ids) == 1:
        message = f"Request #{request_ids[0]} {verb}."
        if not approve:
            message += " Inventory was not changed."
        elif not locked_requests[0].is_archive:
            product = locked_requests[0].product
            message += f" {product.name} stock is now {product.quantity}."
    else:
        message = f"{len(request_ids)} requests {verb}."
    flash(message, "success")
    return redirect(url_for("inventory.manager_dashboard"))


@inventory_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@manager_required
def approve_request(request_id):
    return decide_requests([request_id], approve=True)


@inventory_bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@manager_required
def reject_request(request_id):
    return decide_requests([request_id], approve=False)


@inventory_bp.route("/requests/bulk-decision", methods=["POST"])
@manager_required
def bulk_decision():
    """Approve or reject every request ticked on the manager dashboard."""
    request_ids = sorted({int(i) for i in request.form.getlist("request_ids") if i.isdigit()})
    decision = request.form.get("decision")
    if not request_ids:
        flash("Select at least one request first.", "warning")
        return redirect(url_for("inventory.manager_dashboard"))
    if decision not in ("approve", "reject"):
        abort(400)
    return decide_requests(request_ids, approve=(decision == "approve"))
