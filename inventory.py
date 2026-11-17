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
    DIRECTION_INCREASE,
    REQUEST_ARCHIVE,
    REQUEST_RESTOCK,
    REQUEST_TYPE_LABELS,
    STATUS_PARTIALLY_RECEIVED,
    STATUS_RECEIVED,
    InventoryRequest,
    Product,
    StockMovement,
    User,
    db,
    log_activity,
)
from csv_export import MAX_EXPORT_ROWS, csv_response
from products import safe_next_url
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
        return redirect(url_for("manager.overview"))
    return redirect(url_for("inventory.employee_dashboard"))


def inventory_filters():
    """Search and filters from the URL, e.g. /inventory?q=tape&category=Other"""
    return {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", ""),
        "status": request.args.get("status", ""),
        "show": request.args.get("show", "active"),  # "active" or "archived"
    }


def filter_products(filters):
    """Products matching the Inventory page filters (used by the page and its CSV export)."""
    query = Product.query.filter_by(is_archived=filters["show"] == "archived")
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
    return products


@inventory_bp.route("/inventory")
@login_required
def inventory_list():
    filters = inventory_filters()
    showing_archived = filters["show"] == "archived"
    products = filter_products(filters)

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
        category="damage_report",  # Damaged or Missing items
    )
    db.session.commit()

    flash("Report submitted. A manager will review it.", "success")
    return redirect(url_for("inventory.employee_dashboard"))


# ---------------------------------------------------------------------------
# Manager pages
# ---------------------------------------------------------------------------

REQUEST_STATUSES = [
    STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_PARTIALLY_RECEIVED, STATUS_RECEIVED,
]
APPROVALS_PER_PAGE = 25


def request_type_condition(type_key):
    """The SQL filter for one request type (see REQUEST_TYPE_LABELS in models.py)."""
    is_adjustment = InventoryRequest.request_type == REQUEST_ADJUSTMENT
    return {
        "archive": InventoryRequest.request_type == REQUEST_ARCHIVE,
        "restock": InventoryRequest.request_type == REQUEST_RESTOCK,
        "damaged": db.and_(is_adjustment, InventoryRequest.reason == "Damaged"),
        "missing": db.and_(is_adjustment, InventoryRequest.reason == "Missing"),
        "increase": db.and_(is_adjustment, InventoryRequest.direction == DIRECTION_INCREASE),
        "decrease": db.and_(
            is_adjustment,
            InventoryRequest.direction == DIRECTION_DECREASE,
            InventoryRequest.reason.notin_(["Damaged", "Missing"]),
        ),
    }[type_key]


def approval_filters():
    filters = {
        "status": request.args.get("status", STATUS_PENDING),  # "all" shows every status
        "type": request.args.get("type", ""),
        "employee": request.args.get("employee", type=int),
    }
    if filters["status"] not in REQUEST_STATUSES:
        filters["status"] = "all"
    return filters


def requests_matching(filters):
    """Requests matching the type and employee filters (status is applied by the caller)."""
    query = InventoryRequest.query
    if filters["type"] in REQUEST_TYPE_LABELS:
        query = query.filter(request_type_condition(filters["type"]))
    if filters["employee"]:
        query = query.filter(InventoryRequest.employee_id == filters["employee"])
    return query


# "/manager" was this page's old address; it still works so old links don't break.
@inventory_bp.route("/manager")
@inventory_bp.route("/approvals")  # registered first, so url_for() builds /approvals
@manager_required
def manager_dashboard():
    """Approvals: every employee request, with filters. Pending is shown by default."""
    filters = approval_filters()
    query = requests_matching(filters)

    # Counts for the status tabs (respecting the type/employee filters).
    status_counts = dict(
        query.with_entities(InventoryRequest.status, db.func.count())
        .group_by(InventoryRequest.status)
        .all()
    )
    status_counts["all"] = sum(status_counts.values())

    if filters["status"] != "all":
        query = query.filter(InventoryRequest.status == filters["status"])
    if filters["status"] == STATUS_PENDING:
        # The review queue: oldest first, so nothing waits forever.
        query = query.order_by(InventoryRequest.created_at, InventoryRequest.id)
    else:
        query = query.order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())

    page = query.paginate(
        page=request.args.get("page", 1, type=int), per_page=APPROVALS_PER_PAGE, error_out=False
    )
    employees = (
        User.query.filter(User.id.in_(db.select(InventoryRequest.employee_id)))
        .order_by(User.username)
        .all()
    )
    return render_template(
        "approvals.html",
        requests=page.items,
        page=page,
        filters=filters,
        status_counts=status_counts,
        statuses=REQUEST_STATUSES,
        type_labels=REQUEST_TYPE_LABELS,
        employees=employees,
        has_pending=any(r.status == STATUS_PENDING for r in page.items),
    )


@inventory_bp.route("/requests/<int:request_id>")
@login_required
def request_detail(request_id):
    """
    One request with everything about it. Managers can see any request;
    employees only their own (others get "not found", so request ids from
    other employees can't even be confirmed to exist).
    """
    inventory_request = db.get_or_404(InventoryRequest, request_id)
    if not g.user.is_manager and inventory_request.employee_id != g.user.id:
        abort(404)
    movement = StockMovement.query.filter_by(request_id=inventory_request.id).first()
    return render_template(
        "request_detail.html",
        req=inventory_request,
        movement=movement,
        receipts=inventory_request.receipts,
    )


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
    # Nobody reviews their own request (managers don't normally create
    # requests, but this keeps the rule true even if that ever changes).
    if inventory_request.employee_id == g.user.id:
        raise StockError(f"You can't review your own request #{inventory_request.id}.")

    if approve:
        if product.is_archived:
            raise StockError(f"{product.name} is archived.")
        if inventory_request.is_archive:
            archive_product(product, g.user.id, f"approved request #{inventory_request.id}")
        elif inventory_request.is_restock:
            # Approving a restock only AUTHORIZES the order. Stock goes up
            # later, when a manager records the delivery ("Receive stock").
            pass
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
        category="request_approved" if approve else "request_rejected",
    )


def back_to_review():
    """After a decision, return to the page it was made on (Overview or Requests)."""
    return safe_next_url(url_for("inventory.manager_dashboard"))


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
        return redirect(back_to_review())

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
            return redirect(back_to_review())

        # Lock every affected product too (request rows first, then product
        # rows -- the same order everywhere, which avoids deadlocks).
        products = lock_products({r.product_id for r in locked_requests})
        for inventory_request in locked_requests:
            apply_decision(inventory_request, products[inventory_request.product_id], approve, manager_note)

        db.session.commit()  # saves everything and releases the row locks
    except StockError as problem:
        db.session.rollback()
        if len(request_ids) == 1:
            prefix = f"Cannot {'approve' if approve else 'reject'} request #{request_ids[0]}: "
        else:
            prefix = f"Nothing was {'approved' if approve else 'rejected'}. "
        hint = " Reject that request or adjust stock first." if approve else ""
        flash(f"{prefix}{problem}{hint}", "error")
        return redirect(back_to_review())
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
        elif locked_requests[0].is_restock:
            message += " The restock is authorized; stock goes up when it is received."
        elif not locked_requests[0].is_archive:
            product = locked_requests[0].product
            message += f" {product.name} stock is now {product.quantity}."
    else:
        message = f"{len(request_ids)} requests {verb}."
    flash(message, "success")
    return redirect(back_to_review())


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
        return redirect(back_to_review())
    if decision not in ("approve", "reject"):
        abort(400)
    return decide_requests(request_ids, approve=(decision == "approve"))


# ---------------------------------------------------------------------------
# CSV exports (managers only). Each one uses the same filters as its page.
# ---------------------------------------------------------------------------

@inventory_bp.route("/inventory/export.csv")
@manager_required
def export_inventory():
    products = filter_products(inventory_filters())
    header = ["SKU", "Name", "Category", "Unit", "Quantity", "Reorder level", "Target stock level",
              "Status", "Location", "Supplier", "Unit cost", "Stock value", "Archived",
              "Created (UTC)", "Last updated (UTC)"]
    rows = (
        [
            p.sku, p.name, p.category, p.unit, p.quantity, p.reorder_level, p.target_stock_level,
            "Archived" if p.is_archived else p.stock_status, p.location, p.supplier,
            p.unit_cost, (p.unit_cost * p.quantity) if p.unit_cost is not None else None,
            p.is_archived, p.created_at, p.updated_at,
        ]
        for p in products
    )
    return csv_response("stockflow-inventory", header, rows)


@inventory_bp.route("/approvals/export.csv")
@manager_required
def export_requests():
    filters = approval_filters()
    query = requests_matching(filters)
    if filters["status"] != "all":
        query = query.filter(InventoryRequest.status == filters["status"])
    requests_found = (
        query.order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(MAX_EXPORT_ROWS).all()
    )
    return requests_csv(requests_found, "stockflow-requests")


def requests_csv(requests_found, filename):
    """CSV download of inventory requests (shared with the Reports page)."""
    header = ["Request ID", "Submitted (UTC)", "Employee", "Product SKU", "Product", "Type",
              "Quantity", "Received", "Reason", "Explanation", "Status", "Reviewed by", "Manager notes"]
    rows = (
        [
            r.id, r.created_at, r.employee.username, r.product.sku, r.product.name, r.type_label,
            r.signed_quantity, r.received_quantity if r.is_restock else None,
            None if r.is_archive else r.reason, r.notes, r.status,
            r.manager.username if r.manager else None, r.manager_note,
        ]
        for r in requests_found
    )
    return csv_response(filename, header, rows)
