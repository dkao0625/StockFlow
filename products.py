"""
Product pages.

  /products/new             Add a product            (employees and managers)
  /products/<id>            Product details          (everyone logged in)
  /products/<id>/edit       Edit product details     (managers only)
  /products/<id>/restock    Add stock                (managers only)

Product creation is kept separate from inventory adjustment requests:
employees can add NEW products, but can't edit products or add stock to
existing ones. They use damage/missing reports
instead, which a manager must approve (see inventory.py).
"""

import re
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from auth import login_required, manager_required
from models import (
    PRODUCT_CATEGORIES,
    UNITS_OF_MEASUREMENT,
    InventoryRequest,
    Product,
    StockMovement,
    db,
    log_activity,
)

products_bp = Blueprint("products", __name__, url_prefix="/products")

MAX_STOCK_QUANTITY = 1_000_000
MAX_RESTOCK_QUANTITY = 100_000
MAX_UNIT_COST = Decimal("99999999.99")  # largest value NUMERIC(10, 2) can hold
SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,39}$")

# Maximum lengths -- these match the column sizes in models.py.
MAX_LENGTHS = {"name": 120, "sku": 40, "description": 1000, "location": 100, "supplier": 120}

PRODUCT_FORM_FIELDS = [
    "name", "sku", "category", "description", "unit",
    "quantity", "reorder_level", "location", "supplier", "unit_cost",
]


# ---------------------------------------------------------------------------
# Validation helpers. Each returns (value, error_message_or_None).
# ---------------------------------------------------------------------------

def parse_whole_number(text, label, maximum):
    """A required whole number from 0 to maximum."""
    if text == "":
        return None, f"{label} is required."
    try:
        number = int(text)
    except ValueError:
        return None, f"{label} must be a whole number (no decimals or letters)."
    if number < 0:
        return None, f"{label} can't be negative."
    if number > maximum:
        return None, f"{label} can't be more than {maximum:,}."
    return number, None


def parse_unit_cost(text):
    """An optional dollar amount like 12.50, returned as an exact Decimal."""
    if text == "":
        return None, None  # optional field
    cleaned = text.removeprefix("$").replace(",", "")
    try:
        # Decimal (not float) keeps money exact: Decimal("0.10") is exactly 0.10.
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None, "Enter a dollar amount, like 12.50."
    if not amount.is_finite():  # rejects "NaN" and "Infinity"
        return None, "Enter a dollar amount, like 12.50."
    if amount < 0:
        return None, "Unit cost can't be negative."
    if amount.as_tuple().exponent < -2:
        return None, "Use at most 2 decimal places (cents)."
    if amount > MAX_UNIT_COST:
        return None, "Unit cost is too large."
    return amount.quantize(Decimal("0.01")), None


def validate_product_form(form, existing_product=None):
    """
    Check every field of the add/edit product form ON THE SERVER.

    Returns three things:
      form_values -- what the user typed (to re-fill the form on errors)
      cleaned     -- validated values, ready to save
      errors      -- {field_name: message} shown next to each bad field

    existing_product is given when editing; starting quantity is then
    skipped (stock only changes through stock movements) and the product's
    own SKU doesn't count as a duplicate.
    """
    form_values = {field: form.get(field, "").strip() for field in PRODUCT_FORM_FIELDS}
    form_values["sku"] = form_values["sku"].upper()
    cleaned = {}
    errors = {}

    # Required text fields
    if not form_values["name"]:
        errors["name"] = "Product name is required."
    cleaned["name"] = form_values["name"]

    if not form_values["sku"]:
        errors["sku"] = "SKU is required."
    elif not SKU_PATTERN.match(form_values["sku"]):
        errors["sku"] = "Use 2-40 letters, numbers, or dashes (for example SF-2001)."
    else:
        duplicate_query = Product.query.filter_by(sku=form_values["sku"])
        if existing_product is not None:
            duplicate_query = duplicate_query.filter(Product.id != existing_product.id)
        if duplicate_query.first() is not None:
            errors["sku"] = f"SKU {form_values['sku']} is already used by another product."
    cleaned["sku"] = form_values["sku"]

    # Dropdowns: only accept values from our fixed lists, even though the
    # browser shows a dropdown (someone could still send any text).
    if form_values["category"] not in PRODUCT_CATEGORIES:
        errors["category"] = "Choose a category from the list."
    cleaned["category"] = form_values["category"]

    if form_values["unit"] not in UNITS_OF_MEASUREMENT:
        errors["unit"] = "Choose a unit of measurement from the list."
    cleaned["unit"] = form_values["unit"]

    # Numbers
    if existing_product is None:
        cleaned["quantity"], error = parse_whole_number(
            form_values["quantity"], "Starting quantity", MAX_STOCK_QUANTITY
        )
        if error:
            errors["quantity"] = error

    cleaned["reorder_level"], error = parse_whole_number(
        form_values["reorder_level"], "Reorder level", MAX_STOCK_QUANTITY
    )
    if error:
        errors["reorder_level"] = error

    # Unit cost is manager-only information. If an employee sends one anyway
    # (the field is hidden from them), it is ignored rather than saved.
    if g.user.is_manager:
        cleaned["unit_cost"], error = parse_unit_cost(form_values["unit_cost"])
        if error:
            errors["unit_cost"] = error
    else:
        form_values["unit_cost"] = ""
        cleaned["unit_cost"] = None

    # Optional text fields: empty text is saved as NULL (no value)
    for field in ["description", "location", "supplier"]:
        cleaned[field] = form_values[field] or None

    # Length limits for every text field
    for field, max_length in MAX_LENGTHS.items():
        if len(form_values[field]) > max_length and field not in errors:
            errors[field] = f"Must be {max_length} characters or fewer."

    return form_values, cleaned, errors


def render_product_form(product, form_values, errors, status_code=200):
    return render_template(
        "product_form.html",
        product=product,  # None when adding a new product
        form=form_values,
        errors=errors,
        categories=PRODUCT_CATEGORIES,
        units=UNITS_OF_MEASUREMENT,
    ), status_code


def get_product_or_404(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    return product


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@products_bp.route("/new", methods=["GET", "POST"])
@login_required  # any logged-in employee or manager can add a new product
def new_product():
    if request.method == "GET":
        empty_form = {"category": "", "unit": "Pieces", "quantity": "0", "reorder_level": "0"}
        return render_product_form(None, empty_form, {})

    form_values, cleaned, errors = validate_product_form(request.form)
    if errors:
        flash("Please fix the highlighted fields below.", "error")
        return render_product_form(None, form_values, errors, status_code=400)

    # id, created_at and updated_at are filled in automatically.
    product = Product(**cleaned)
    db.session.add(product)
    db.session.flush()  # assigns product.id

    # A new product with stock gets a starting stock movement, so the
    # product's stock history adds up to its current quantity.
    if product.quantity > 0:
        db.session.add(
            StockMovement(
                product_id=product.id,
                quantity_change=product.quantity,
                actor_id=g.user.id,
                note="Starting stock",
            )
        )
    log_activity(g.user.id, f"Added product {product.sku} ({product.name}) with {product.quantity} {product.unit.lower()}")

    try:
        db.session.commit()
    except IntegrityError:
        # Two managers saved the same SKU at the same moment; the database's
        # UNIQUE constraint caught the second one.
        db.session.rollback()
        errors = {"sku": f"SKU {cleaned['sku']} is already used by another product."}
        flash("Please fix the highlighted fields below.", "error")
        return render_product_form(None, form_values, errors, status_code=400)

    flash(f"Product saved: {product.name} ({product.sku}).", "success")
    return redirect(url_for("products.detail", product_id=product.id))


@products_bp.route("/<int:product_id>")
@login_required
def detail(product_id):
    product = get_product_or_404(product_id)

    stock_history = (
        StockMovement.query.filter_by(product_id=product.id)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(100)
        .all()
    )

    # Managers see every report for this product. Employees see ONLY their
    # own -- the same privacy rule as the employee dashboard.
    requests_query = InventoryRequest.query.filter_by(product_id=product.id)
    if not g.user.is_manager:
        requests_query = requests_query.filter_by(employee_id=g.user.id)
    product_requests = requests_query.order_by(
        InventoryRequest.created_at.desc(), InventoryRequest.id.desc()
    ).all()

    return render_template(
        "product.html",
        product=product,
        stock_history=stock_history,
        product_requests=product_requests,
    )


@products_bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@manager_required
def edit(product_id):
    product = get_product_or_404(product_id)

    if request.method == "GET":
        current_values = {
            field: "" if getattr(product, field) is None else str(getattr(product, field))
            for field in PRODUCT_FORM_FIELDS
        }
        return render_product_form(product, current_values, {})

    form_values, cleaned, errors = validate_product_form(request.form, existing_product=product)
    if errors:
        flash("Please fix the highlighted fields below.", "error")
        return render_product_form(product, form_values, errors, status_code=400)

    # Quantity is NOT edited here: stock changes go through "Add stock" or
    # approved reports, so every change is recorded in the stock history.
    for field, value in cleaned.items():
        setattr(product, field, value)  # updated_at is set automatically
    log_activity(g.user.id, f"Edited product {product.sku}")

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        errors = {"sku": f"SKU {cleaned['sku']} is already used by another product."}
        flash("Please fix the highlighted fields below.", "error")
        return render_product_form(product, form_values, errors, status_code=400)

    flash(f"Changes saved for {product.name}.", "success")
    return redirect(url_for("products.detail", product_id=product.id))


@products_bp.route("/<int:product_id>/restock", methods=["POST"])
@manager_required  # employees can't change stock directly
def restock(product_id):
    quantity, error = parse_whole_number(
        request.form.get("quantity", "").strip(), "Quantity to add", MAX_RESTOCK_QUANTITY
    )
    if error is None and quantity == 0:
        error = "Quantity to add must be at least 1."
    note = request.form.get("note", "").strip()
    if error is None and len(note) > 255:
        error = "Note must be 255 characters or fewer."
    if error:
        flash(error, "error")
        return redirect(url_for("products.detail", product_id=product_id))

    try:
        # Lock the product row (SELECT ... FOR UPDATE) so a restock and an
        # approval happening at the same moment can't overwrite each other.
        # Without the lock, both could read 10, and one change would be lost.
        product = db.session.execute(
            db.select(Product)
            .where(Product.id == product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if product is None:
            db.session.rollback()
            abort(404)

        if product.quantity + quantity > MAX_STOCK_QUANTITY:
            db.session.rollback()
            flash(f"Stock can't go above {MAX_STOCK_QUANTITY:,}.", "error")
            return redirect(url_for("products.detail", product_id=product_id))

        # Stock calculation: add the received quantity. The status (In/Low/Out
        # of Stock) is recalculated automatically from the new quantity.
        product.quantity += quantity
        db.session.add(
            StockMovement(
                product_id=product.id,
                quantity_change=quantity,  # positive = stock added
                actor_id=g.user.id,
                note=note or "Stock added",
            )
        )
        log_activity(g.user.id, f"Added {quantity} to {product.sku} (now {product.quantity})")
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"Added {quantity} {product.unit.lower()}. {product.name} stock is now {product.quantity}.", "success")
    return redirect(url_for("products.detail", product_id=product_id))
