"""
Bulk actions on the products selected on the inventory page.

  /bulk/edit      Change category / unit / location / supplier (+ reorder
                  level for managers) on many products at once.
  /bulk/adjust    Stock increases/decreases. Employees create Pending
                  requests for a manager; managers apply them directly.
  /bulk/archive   "Delete". Employees create archive requests; managers
                  archive directly. Nothing is ever permanently deleted.

Each page works in two steps:
  GET  shows the selected products and a form (nothing changes yet)
  POST validates everything, then saves it all in ONE transaction --
       if anything is invalid, nothing is saved.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import login_required
from models import (
    ARCHIVE_REASON,
    DECREASE_REASONS,
    DIRECTION_DECREASE,
    DIRECTION_INCREASE,
    INCREASE_REASONS,
    PRODUCT_CATEGORIES,
    REQUEST_ADJUSTMENT,
    REQUEST_ARCHIVE,
    STATUS_PENDING,
    UNITS_OF_MEASUREMENT,
    InventoryRequest,
    Product,
    db,
    log_activity,
)
from products import describe_changes, parse_whole_number, safe_next_url
from stock import (
    MAX_STOCK_QUANTITY,
    StockError,
    archive_blocker,
    archive_product,
    change_stock,
    lock_products,
)

bulk_bp = Blueprint("bulk", __name__, url_prefix="/bulk")

MAX_SELECTION = 200
MAX_ADJUST_QUANTITY = 100_000
MAX_ADJUST_NOTE = 200
MAX_ARCHIVE_NOTE = 500

# Bulk edit: which fields each role may change.
EMPLOYEE_BULK_FIELDS = ["category", "unit", "location", "supplier"]
MANAGER_BULK_FIELDS = EMPLOYEE_BULK_FIELDS + ["reorder_level"]
ALL_BULK_FIELDS = MANAGER_BULK_FIELDS


# ---------------------------------------------------------------------------
# Reading the selection
# ---------------------------------------------------------------------------

def selected_ids():
    """The product ids ticked on the inventory page (?product_ids=1&product_ids=4)."""
    ids = set()
    for raw_id in request.values.getlist("product_ids"):
        if not raw_id.isdigit():
            abort(400)  # not a number: a hand-crafted request
        ids.add(int(raw_id))
    return sorted(ids)


def load_selection(lock=False):
    """
    Validate the selected ids and return their products (in id order), or
    None after flashing an error. With lock=True the rows are locked
    (SELECT ... FOR UPDATE) for the rest of the transaction.
    """
    ids = selected_ids()
    if not ids:
        flash("Select at least one product first.", "warning")
        return None
    if len(ids) > MAX_SELECTION:
        flash(f"You can select up to {MAX_SELECTION} products at a time.", "error")
        return None

    if lock:
        products_by_id = lock_products(ids)
    else:
        products_by_id = {p.id: p for p in Product.query.filter(Product.id.in_(ids))}

    # Every id must be a real, active product.
    if any(i not in products_by_id or products_by_id[i].is_archived for i in ids):
        if lock:
            db.session.rollback()
        flash("Some selected products no longer exist or were archived. Please select again.", "error")
        return None
    return [products_by_id[i] for i in ids]


def back_to_inventory():
    return redirect(safe_next_url(url_for("inventory.inventory_list")))


# ---------------------------------------------------------------------------
# Bulk edit
# ---------------------------------------------------------------------------

def bulk_fields_for(user):
    return MANAGER_BULK_FIELDS if user.is_manager else EMPLOYEE_BULK_FIELDS


def validate_bulk_edit(form, enabled_fields):
    """Returns (new_values, errors) for the fields the user ticked."""
    new_values, errors = {}, {}
    for field in enabled_fields:
        value = form.get(field, "").strip()
        if field == "category":
            if value not in PRODUCT_CATEGORIES:
                errors[field] = "Choose a category from the list."
            new_values[field] = value
        elif field == "unit":
            if value not in UNITS_OF_MEASUREMENT:
                errors[field] = "Choose a unit from the list."
            new_values[field] = value
        elif field in ("location", "supplier"):
            max_length = 100 if field == "location" else 120
            if len(value) > max_length:
                errors[field] = f"Must be {max_length} characters or fewer."
            new_values[field] = value or None  # blank clears the field
        elif field == "reorder_level":
            new_values[field], error = parse_whole_number(value, "Reorder level", MAX_STOCK_QUANTITY)
            if error:
                errors[field] = error
    return new_values, errors


def render_bulk_edit(products, form, errors, status_code=200):
    return render_template(
        "bulk_edit.html",
        products=products,
        form=form,
        errors=errors,
        fields=bulk_fields_for(g.user),
        categories=PRODUCT_CATEGORIES,
        units=UNITS_OF_MEASUREMENT,
        next_url=safe_next_url(url_for("inventory.inventory_list")),
    ), status_code


@bulk_bp.route("/edit", methods=["GET", "POST"])
@login_required
def edit():
    allowed_fields = bulk_fields_for(g.user)

    if request.method == "GET":
        products = load_selection()
        if products is None:
            return back_to_inventory()
        return render_bulk_edit(products, {}, {})

    # Which fields did the user switch on ("Update this field")?
    enabled_fields = [f for f in ALL_BULK_FIELDS if request.form.get(f"update_{f}")]
    # Reject fields this role may not change (e.g. an employee sending
    # update_reorder_level). The page doesn't offer them, so this is tampering.
    if any(f not in allowed_fields for f in enabled_fields):
        abort(403)

    new_values, errors = validate_bulk_edit(request.form, enabled_fields)
    if not enabled_fields:
        errors["_form"] = "Switch on at least one field to update."

    try:
        products = load_selection(lock=True)
        if products is None:
            return back_to_inventory()
        if errors:
            db.session.rollback()
            flash(errors.get("_form", "Please fix the highlighted fields below."), "error")
            return render_bulk_edit(products, request.form, errors, status_code=400)

        changed_count = 0
        for product in products:
            # ONLY the switched-on fields are touched. Everything else on
            # the product (name, SKU, quantity, ...) stays exactly the same.
            changes = describe_changes(product, new_values)
            if not changes:
                continue
            for field, value in new_values.items():
                setattr(product, field, value)
            # Who changed which fields on which product (time is automatic).
            log_activity(g.user.id, f"Bulk edited {product.sku}: {changes}", product.id)
            changed_count += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"Updated {changed_count} of {len(products)} selected product(s).", "success")
    return back_to_inventory()


# ---------------------------------------------------------------------------
# Bulk stock adjustment
# ---------------------------------------------------------------------------

def read_adjustment_rows(products):
    """
    Read one adjustment per product from fields named like quantity_<id>.
    Returns (rows, errors) where errors maps product id -> message.
    """
    rows, errors = {}, {}
    for product in products:
        direction = request.form.get(f"direction_{product.id}", "")
        reason = request.form.get(f"reason_{product.id}", "")
        note = request.form.get(f"note_{product.id}", "").strip()
        quantity, error = parse_whole_number(
            request.form.get(f"quantity_{product.id}", "").strip(), "Quantity", MAX_ADJUST_QUANTITY
        )
        if error is None and quantity == 0:
            error = "Quantity must be at least 1."
        elif direction not in (DIRECTION_INCREASE, DIRECTION_DECREASE):
            error = "Choose increase or decrease."
        elif reason not in (INCREASE_REASONS if direction == DIRECTION_INCREASE else DECREASE_REASONS):
            error = f"'{reason or 'No reason'}' doesn't fit a stock {direction}. Choose a reason from the list."
        elif len(note) > MAX_ADJUST_NOTE:
            error = f"Note must be {MAX_ADJUST_NOTE} characters or fewer."
        elif direction == DIRECTION_DECREASE and quantity > product.quantity:
            # Stock can never go negative.
            error = f"Only {product.quantity} in stock."
        if error:
            errors[product.id] = error
        rows[product.id] = {"direction": direction, "reason": reason, "quantity": quantity, "note": note}
    return rows, errors


def render_bulk_adjust(products, errors, status_code=200):
    return render_template(
        "bulk_adjust.html",
        products=products,
        form=request.form,
        errors=errors,
        increase_reasons=INCREASE_REASONS,
        decrease_reasons=DECREASE_REASONS,
        next_url=safe_next_url(url_for("inventory.inventory_list")),
    ), status_code


@bulk_bp.route("/adjust", methods=["GET", "POST"])
@login_required
def adjust():
    if request.method == "GET":
        products = load_selection()
        if products is None:
            return back_to_inventory()
        return render_bulk_adjust(products, {})

    try:
        products = load_selection(lock=True)
        if products is None:
            return back_to_inventory()
        rows, errors = read_adjustment_rows(products)
        if errors:
            db.session.rollback()
            flash("Please fix the highlighted rows. Nothing was saved.", "error")
            return render_bulk_adjust(products, errors, status_code=400)

        for product in products:
            row = rows[product.id]
            if g.user.is_manager:
                # Managers may change stock directly (the same rule as
                # "Add stock"). change_stock records a stock movement.
                amount = row["quantity"] if row["direction"] == DIRECTION_INCREASE else -row["quantity"]
                note = f"{row['reason']}: {row['note']}" if row["note"] else row["reason"]
                change_stock(product, amount, g.user.id, note=note)
            else:
                # Employees NEVER change stock directly: they create a Pending
                # request that goes into the manager's approval queue.
                new_request = InventoryRequest(
                    product_id=product.id,
                    employee_id=g.user.id,
                    request_type=REQUEST_ADJUSTMENT,
                    direction=row["direction"],
                    quantity=row["quantity"],
                    reason=row["reason"],
                    notes=row["note"],
                    status=STATUS_PENDING,
                )
                db.session.add(new_request)
                db.session.flush()
                log_activity(g.user.id, f"Requested stock change #{new_request.id}: {new_request.summary} for {product.sku}", product.id)
        db.session.commit()
    except StockError as problem:
        db.session.rollback()
        flash(f"Nothing was saved. {problem}", "error")
        return back_to_inventory()
    except Exception:
        db.session.rollback()
        raise

    if g.user.is_manager:
        flash(f"Stock adjusted for {len(products)} product(s).", "success")
        return back_to_inventory()
    flash(f"Submitted {len(products)} stock adjustment request(s). A manager will review them.", "success")
    return redirect(url_for("inventory.employee_dashboard"))


# ---------------------------------------------------------------------------
# Bulk delete (archive)
# ---------------------------------------------------------------------------

def archive_blockers(products):
    """{product_id: reason} for selected products that can't be archived now."""
    blockers = {}
    for product in products:
        reason = archive_blocker(product)
        if reason is None and not g.user.is_manager:
            already_requested = InventoryRequest.query.filter_by(
                product_id=product.id, status=STATUS_PENDING, request_type=REQUEST_ARCHIVE
            ).first()
            if already_requested:
                reason = "already has a pending deletion request"
        if reason:
            blockers[product.id] = reason
    return blockers


@bulk_bp.route("/archive", methods=["GET", "POST"])
@login_required
def archive():
    if request.method == "GET":
        products = load_selection()
        if products is None:
            return back_to_inventory()
        return render_template(
            "bulk_archive.html",
            products=products,
            blockers=archive_blockers(products),
            next_url=safe_next_url(url_for("inventory.inventory_list")),
        )

    note = request.form.get("note", "").strip()
    if len(note) > MAX_ARCHIVE_NOTE:
        flash(f"Reason must be {MAX_ARCHIVE_NOTE} characters or fewer.", "error")
        return redirect(url_for("bulk.archive", product_ids=selected_ids()))
    # Managers must tick the extra "I understand" box on the confirmation page.
    if g.user.is_manager and request.form.get("acknowledge") != "yes":
        flash("Tick the confirmation box to archive these products.", "error")
        return redirect(url_for("bulk.archive", product_ids=selected_ids(), next=request.form.get("next", "")))

    try:
        products = load_selection(lock=True)
        if products is None:
            return back_to_inventory()
        # Check again now that the rows are locked: a new request may have
        # arrived since the confirmation page was shown.
        blockers = archive_blockers(products)
        if blockers:
            db.session.rollback()
            names = ", ".join(p.name for p in products if p.id in blockers)
            flash(f"Nothing was archived. These products can't be archived right now: {names}.", "error")
            return redirect(url_for("bulk.archive", product_ids=[p.id for p in products]))

        for product in products:
            if g.user.is_manager:
                archive_product(product, g.user.id, note or "archived by manager")
            else:
                # Employees can't archive. They request it; a manager decides.
                new_request = InventoryRequest(
                    product_id=product.id,
                    employee_id=g.user.id,
                    request_type=REQUEST_ARCHIVE,
                    reason=ARCHIVE_REASON,
                    notes=note,
                    status=STATUS_PENDING,
                )
                db.session.add(new_request)
                db.session.flush()
                log_activity(g.user.id, f"Requested deletion #{new_request.id} of {product.sku}", product.id)
        db.session.commit()
    except StockError as problem:
        db.session.rollback()
        flash(f"Nothing was archived. {problem}", "error")
        return back_to_inventory()
    except Exception:
        db.session.rollback()
        raise

    if g.user.is_manager:
        flash(f"Archived {len(products)} product(s). Their history is kept.", "success")
        return back_to_inventory()
    flash(f"Deletion requested for {len(products)} product(s). A manager will review it.", "success")
    return redirect(url_for("inventory.employee_dashboard"))
