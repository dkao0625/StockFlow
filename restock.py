"""
Restock workflow.

  1. Someone asks for more stock: a restock request with a quantity and reason.
       - Employee -> status "Pending" (a manager must approve it).
       - Manager  -> status "Approved" right away (they have the authority).
  2. Manager approves it on the Approvals page. This AUTHORIZES the order
     but does NOT change inventory -- nothing has arrived yet.
  3. When the delivery arrives, a manager clicks "Receive stock" and enters
     how many units came. Inventory goes up, a stock movement and a receipt
     are recorded, and the request becomes "Partially Received" or "Received".

You can't receive more than the approved quantity that is still to come.
Extra units must be added with a separate, explicit "Add stock" on the
product page (which is recorded with its own note).
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import login_required, manager_required
from inventory import MAX_NOTE_LENGTH, MAX_REQUEST_QUANTITY, lock_requests, parse_positive_quantity
from models import (
    DIRECTION_INCREASE,
    REQUEST_RESTOCK,
    RESTOCK_REASON,
    EVENT_CLOSED,
    EVENT_RECEIVED,
    STATUS_APPROVED,
    STATUS_NEEDS_CORRECTION,
    STATUS_PARTIALLY_RECEIVED,
    STATUS_PENDING,
    STATUS_RECEIVED,
    InventoryRequest,
    Product,
    StockReceipt,
    db,
    log_activity,
    record_event,
)
from products import safe_next_url
from stock import StockError, change_stock, lock_products

restock_bp = Blueprint("restock", __name__, url_prefix="/restock")

MAX_RECEIPT_NOTE_LENGTH = 255
OPEN_STATUSES = [STATUS_APPROVED, STATUS_PARTIALLY_RECEIVED]  # waiting for delivery

# ?show=... on the Restock orders page -> (tab label, statuses)
RESTOCK_TABS = {
    "awaiting": ("Awaiting delivery", OPEN_STATUSES),
    "pending": ("Pending approval", [STATUS_PENDING]),
    "received": ("Received", [STATUS_RECEIVED]),
    "all": ("All", None),
}


def on_order_by_product():
    """
    {product_id: units} still expected for each product: pending restock
    requests plus approved units not yet received. Shown on Stock Alerts so
    nobody orders the same thing twice.
    """
    rows = (
        db.session.query(
            InventoryRequest.product_id,
            db.func.sum(InventoryRequest.quantity - InventoryRequest.received_quantity),
        )
        .filter(
            InventoryRequest.request_type == REQUEST_RESTOCK,
            InventoryRequest.status.in_([STATUS_PENDING, STATUS_NEEDS_CORRECTION] + OPEN_STATUSES),
        )
        .group_by(InventoryRequest.product_id)
        .all()
    )
    return {product_id: int(units) for product_id, units in rows if units}


# ---------------------------------------------------------------------------
# 1. Create a restock request (employees and managers)
# ---------------------------------------------------------------------------

def create_restock(product, quantity, notes, additional_notes=None):
    """
    Add a restock request for the logged-in user to the current transaction
    (caller validates the input and commits). Also used by the employee
    dashboard's "Report a problem" form, so there's only one way to make one.
    """
    new_restock = InventoryRequest(
        product_id=product.id,
        employee_id=g.user.id,
        request_type=REQUEST_RESTOCK,
        direction=DIRECTION_INCREASE,
        quantity=quantity,
        reason=RESTOCK_REASON,
        notes=notes,
        additional_notes=additional_notes,
        status=STATUS_PENDING,
    )
    if g.user.is_manager:
        # A manager's own restock is authorized when they create it.
        new_restock.status = STATUS_APPROVED
        new_restock.manager_id = g.user.id
        new_restock.manager_note = "Created and authorized by manager"
    db.session.add(new_restock)
    db.session.flush()  # assigns new_restock.id for the log message
    verb = "Created and authorized" if g.user.is_manager else "Submitted"
    log_activity(g.user.id, f"{verb} restock request #{new_restock.id}: {quantity} x {product.sku}", product.id,
                 category="request_submitted", request_id=new_restock.id)
    return new_restock


@restock_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_request():
    products = Product.query.filter_by(is_archived=False).order_by(Product.name).all()
    form = {
        "product_id": request.values.get("product_id", type=int),
        "quantity": request.form.get("quantity", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }
    selected = db.session.get(Product, form["product_id"]) if form["product_id"] else None

    if request.method == "GET":
        # Suggest a quantity when the product has a target stock level.
        if selected is not None and selected.suggested_reorder_quantity:
            form["quantity"] = str(selected.suggested_reorder_quantity)
        return render_template("restock_form.html", products=products, form=form,
                               selected=selected, on_order=on_order_by_product(), errors={})

    errors = {}
    quantity = parse_positive_quantity(form["quantity"])
    if selected is None or selected.is_archived:
        errors["product_id"] = "Choose an active product."
    if quantity is None:
        errors["quantity"] = f"Enter a whole number from 1 to {MAX_REQUEST_QUANTITY:,}."
    if not form["notes"]:
        errors["notes"] = "Explain why this restock is needed."
    elif len(form["notes"]) > MAX_NOTE_LENGTH:
        errors["notes"] = f"Keep the reason under {MAX_NOTE_LENGTH} characters."
    if errors:
        return render_template("restock_form.html", products=products, form=form,
                               selected=selected, on_order=on_order_by_product(), errors=errors), 400

    new_restock = create_restock(selected, quantity, form["notes"])
    db.session.commit()

    if g.user.is_manager:
        flash(f"Restock #{new_restock.id} for {quantity} {selected.name} is authorized. "
              "Record the delivery with Receive stock when it arrives.", "success")
        return redirect(url_for("restock.orders"))
    flash(f"Restock request #{new_restock.id} submitted. A manager will review it.", "success")
    return redirect(url_for("inventory.employee_dashboard"))


# ---------------------------------------------------------------------------
# 2. Track restock orders (managers)
# ---------------------------------------------------------------------------

@restock_bp.route("")
@manager_required
def orders():
    show = request.args.get("show", "awaiting")
    if show not in RESTOCK_TABS:
        show = "awaiting"

    base = InventoryRequest.query.filter_by(request_type=REQUEST_RESTOCK)
    counts = {
        key: (base.filter(InventoryRequest.status.in_(statuses)) if statuses else base).count()
        for key, (_label, statuses) in RESTOCK_TABS.items()
    }
    statuses = RESTOCK_TABS[show][1]
    query = base.filter(InventoryRequest.status.in_(statuses)) if statuses else base
    restocks = query.order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc()).limit(200).all()
    return render_template("restock_orders.html", restocks=restocks, show=show, tabs=RESTOCK_TABS, counts=counts)


# ---------------------------------------------------------------------------
# 3. Receive stock (managers)
# ---------------------------------------------------------------------------

@restock_bp.route("/<int:request_id>/receive", methods=["POST"])
@manager_required
def receive(request_id):
    back = safe_next_url(url_for("restock.orders"))
    quantity = parse_positive_quantity(request.form.get("quantity", "").strip())
    note = request.form.get("note", "").strip()
    if quantity is None:
        flash(f"Received quantity must be a whole number from 1 to {MAX_REQUEST_QUANTITY:,}.", "error")
        return redirect(back)
    if len(note) > MAX_RECEIPT_NOTE_LENGTH:
        flash(f"Note must be {MAX_RECEIPT_NOTE_LENGTH} characters or fewer.", "error")
        return redirect(back)

    # ---- One database transaction: all saved, or nothing -------------------
    try:
        # Lock the request row first, then the product row (same order as
        # approvals), so two managers receiving at once can't both count
        # the same remaining units.
        locked = lock_requests([request_id])
        if not locked:
            db.session.rollback()
            abort(404)
        restock = locked[0]
        if not restock.can_receive:
            db.session.rollback()
            flash(f"Restock #{request_id} isn't waiting for a delivery (status: {restock.status}).", "warning")
            return redirect(back)
        if quantity > restock.remaining_quantity:
            db.session.rollback()
            flash(
                f"Only {restock.remaining_quantity} unit(s) of restock #{request_id} are still expected. "
                "Receive that amount here, then record any extra units with “Add stock” on the "
                "product page and a note explaining them.",
                "error",
            )
            return redirect(back)

        product = lock_products([restock.product_id])[restock.product_id]
        movement = change_stock(
            product, quantity, g.user.id,
            note=f"Received restock (request #{restock.id})" + (f": {note}" if note else ""),
            log_request_id=restock.id,
        )
        db.session.flush()  # gives the movement an id for the receipt
        db.session.add(StockReceipt(
            request_id=restock.id,
            movement_id=movement.id,
            quantity=quantity,
            received_by_id=g.user.id,
            note=note or None,
        ))
        restock.received_quantity += quantity
        restock.status = STATUS_RECEIVED if restock.remaining_quantity == 0 else STATUS_PARTIALLY_RECEIVED
        record_event(restock, g.user.id, EVENT_RECEIVED, note=note or None, quantity_after=quantity)
        log_activity(
            g.user.id,
            f"Received {quantity} for restock #{restock.id} ({restock.received_quantity} of "
            f"{restock.quantity}, {restock.status})",
            product.id,
            category="stock_received",
            # (the +quantity itself is on the "Stock +N" entry change_stock wrote)
            request_id=restock.id,
        )
        db.session.commit()
    except StockError as problem:
        db.session.rollback()
        flash(f"Nothing was received. {problem}", "error")
        return redirect(back)
    except Exception:
        db.session.rollback()
        raise
    # ---- End of transaction -------------------------------------------------

    flash(f"Received {quantity} {product.unit.lower()} of {product.name}. Stock is now {product.quantity}. "
          f"Restock #{restock.id}: {restock.status}.", "success")
    return redirect(back)


@restock_bp.route("/<int:request_id>/close", methods=["POST"])
@manager_required
def close(request_id):
    """
    Stop waiting for the rest of an order (e.g. the supplier can't deliver).
    Nothing changes in inventory; the request is marked Received with a note
    saying how much actually arrived.
    """
    back = safe_next_url(url_for("restock.orders"))
    reason = request.form.get("note", "").strip()
    if not reason or len(reason) > MAX_NOTE_LENGTH:
        flash("Add a short note (up to 500 characters) explaining why the order is closed.", "error")
        return redirect(back)

    try:
        locked = lock_requests([request_id])
        if not locked:
            db.session.rollback()
            abort(404)
        restock = locked[0]
        if not restock.can_receive:
            db.session.rollback()
            flash(f"Restock #{request_id} is not open.", "warning")
            return redirect(back)
        summary = f"Closed with {restock.received_quantity} of {restock.quantity} received: {reason}"
        restock.status = STATUS_RECEIVED
        restock.manager_note = summary[:MAX_NOTE_LENGTH]
        record_event(restock, g.user.id, EVENT_CLOSED, note=reason)
        log_activity(g.user.id, f"Closed restock #{restock.id}. {summary}", restock.product_id,
                     category="stock_received", request_id=restock.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"Restock #{request_id} closed. Inventory was not changed.", "success")
    return redirect(back)
