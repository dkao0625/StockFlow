"""
Inventory pages and the employee -> manager request workflow.

Workflow:
  1. Employee submits a Damaged/Missing report  -> status "Pending"
  2. Manager approves -> stock is reduced, a StockMovement is recorded
     Manager rejects  -> stock is NOT changed
  3. Employee sees the updated status on their dashboard.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import employee_required, login_required, manager_required
from models import (
    REQUEST_REASONS,
    STOCK_LOW,
    STOCK_OUT,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    InventoryRequest,
    Product,
    StockMovement,
    db,
    log_activity,
)

inventory_bp = Blueprint("inventory", __name__)

MAX_REQUEST_QUANTITY = 100_000
MAX_NOTE_LENGTH = 500


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
    products = Product.query.order_by(Product.name).all()
    return render_template(
        "inventory.html", products=products, stats=inventory_stats(products)
    )


# ---------------------------------------------------------------------------
# Employee pages
# ---------------------------------------------------------------------------

@inventory_bp.route("/employee")
@employee_required
def employee_dashboard():
    products = Product.query.order_by(Product.name).all()
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
    if product is None:
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
    products = Product.query.order_by(Product.name).all()
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
        stats=inventory_stats(products),
    )


def lock_pending_request(request_id):
    """
    Load a request with a row lock (SELECT ... FOR UPDATE).

    While this transaction holds the lock, any other transaction that
    tries to lock the same row must WAIT until we commit or roll back.
    So if two managers click "Approve" at the same moment, the second
    one waits, then sees the status is no longer Pending, and stops.

    Returns the request, or None if it is not (or no longer) pending.
    Raises 404 if the id does not exist.
    """
    inventory_request = db.session.execute(
        db.select(InventoryRequest)
        .where(InventoryRequest.id == request_id)
        .with_for_update()
        # Always use the freshly read (locked) values, never a cached copy.
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if inventory_request is None:
        abort(404)
    if inventory_request.status != STATUS_PENDING:
        return None
    return inventory_request


def read_manager_note():
    note = request.form.get("manager_note", "").strip()
    if len(note) > MAX_NOTE_LENGTH:
        return None, f"Note must be {MAX_NOTE_LENGTH} characters or fewer."
    return note, None


@inventory_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@manager_required
def approve_request(request_id):
    manager_note, error = read_manager_note()
    if error:
        flash(error, "error")
        return redirect(url_for("inventory.manager_dashboard"))

    # ---- Start of the database transaction --------------------------------
    # Everything until commit() happens as one unit: either ALL changes are
    # saved (status, stock, movement, log) or NONE are (rollback).
    try:
        inventory_request = lock_pending_request(request_id)
        if inventory_request is None:
            db.session.rollback()
            flash(f"Request #{request_id} was already processed.", "warning")
            return redirect(url_for("inventory.manager_dashboard"))

        # Lock the product row too, so no other approval can change this
        # product's quantity between our check and our update.
        # (We always lock request first, then product, in every route, which
        # avoids deadlocks.)
        product = db.session.execute(
            db.select(Product)
            .where(Product.id == inventory_request.product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

        # Stock check: never let quantity go below zero.
        if product.quantity < inventory_request.quantity:
            db.session.rollback()
            flash(
                f"Cannot approve request #{request_id}: only {product.quantity} "
                f"unit(s) of {product.name} in stock (requested "
                f"{inventory_request.quantity}). Reject it or adjust stock first.",
                "error",
            )
            return redirect(url_for("inventory.manager_dashboard"))

        # Stock calculation: subtract the approved quantity.
        product.quantity -= inventory_request.quantity

        inventory_request.status = STATUS_APPROVED
        inventory_request.manager_id = g.user.id
        inventory_request.manager_note = manager_note or None

        # Record the change. quantity_change is negative because stock left.
        db.session.add(
            StockMovement(
                product_id=product.id,
                request_id=inventory_request.id,
                quantity_change=-inventory_request.quantity,
                actor_id=g.user.id,
                note=f"{inventory_request.reason} (request #{inventory_request.id})",
            )
        )
        log_activity(
            g.user.id,
            f"Approved request #{request_id}: -{inventory_request.quantity} {product.sku} "
            f"(now {product.quantity})",
        )

        db.session.commit()  # saves everything and releases the row locks
    except Exception:
        # Any unexpected error: undo all partial changes, then re-raise so
        # Flask shows an error page and the problem is logged.
        db.session.rollback()
        raise
    # ---- End of the database transaction ----------------------------------

    flash(f"Request #{request_id} approved. {product.name} stock is now {product.quantity}.", "success")
    return redirect(url_for("inventory.manager_dashboard"))


@inventory_bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@manager_required
def reject_request(request_id):
    manager_note, error = read_manager_note()
    if error is None and not manager_note:
        error = "Please add a note explaining why the request is rejected."
    if error:
        flash(error, "error")
        return redirect(url_for("inventory.manager_dashboard"))

    try:
        # Lock the request so it can't be approved and rejected at once.
        inventory_request = lock_pending_request(request_id)
        if inventory_request is None:
            db.session.rollback()
            flash(f"Request #{request_id} was already processed.", "warning")
            return redirect(url_for("inventory.manager_dashboard"))

        # Rejecting changes ONLY the request. Product stock is untouched.
        inventory_request.status = STATUS_REJECTED
        inventory_request.manager_id = g.user.id
        inventory_request.manager_note = manager_note
        log_activity(g.user.id, f"Rejected request #{request_id}")
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"Request #{request_id} rejected. Inventory was not changed.", "success")
    return redirect(url_for("inventory.manager_dashboard"))
