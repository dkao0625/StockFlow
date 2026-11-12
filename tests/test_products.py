"""Products: adding, editing, details page, adding stock, and status logic."""

from decimal import Decimal

import pytest

from conftest import login, product_id, submit
from models import ActivityLog, InventoryRequest, Product, StockMovement, db

VALID_PRODUCT = {
    "name": "Stretch Wrap Film",
    "sku": "sf-2001",
    "category": "Packaging Supplies",
    "description": "18 in x 1500 ft, clear",
    "unit": "Rolls",
    "quantity": "25",
    "reorder_level": "5",
    "location": "Warehouse A, Aisle 3",
    "supplier": "Pacific Packaging Co.",
    "unit_cost": "12.50",
}


def add(client, **changes):
    return client.post("/products/new", data={**VALID_PRODUCT, **changes})


def count_products(app):
    with app.app_context():
        return Product.query.count()


# ---------------------------------------------------------------------------
# Creating products
# ---------------------------------------------------------------------------

def test_manager_creates_product_with_all_fields(app, client):
    login(client, "boss")
    response = add(client)
    assert response.status_code == 302  # redirected to the new product's page

    with app.app_context():
        product = Product.query.filter_by(sku="SF-2001").one()  # SKU saved in capitals
        assert product.name == "Stretch Wrap Film"
        assert product.category == "Packaging Supplies"
        assert product.unit == "Rolls"
        assert product.description == "18 in x 1500 ft, clear"
        assert product.location == "Warehouse A, Aisle 3"
        assert product.supplier == "Pacific Packaging Co."
        assert product.unit_cost == Decimal("12.50")  # exact decimal, not float
        assert product.quantity == 25 and product.reorder_level == 5
        assert product.created_at is not None and product.updated_at is not None

        # Positive starting stock is recorded in the stock history
        movement = StockMovement.query.filter_by(product_id=product.id).one()
        assert movement.quantity_change == 25
        assert movement.note == "Starting stock"
        assert movement.actor.username == "boss"
        assert ActivityLog.query.filter(ActivityLog.action.like("Added product SF-2001%")).count() == 1
        new_id = product.id

    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Product saved" in page  # success message
    assert f"/products/{new_id}" in response.headers["Location"]


def test_optional_fields_can_be_blank(app, client):
    login(client, "boss")
    add(client, sku="SF-2002", description="", location="", supplier="", unit_cost="", quantity="0")
    with app.app_context():
        product = Product.query.filter_by(sku="SF-2002").one()
        assert product.description is None and product.unit_cost is None
        assert StockMovement.query.filter_by(product_id=product.id).count() == 0  # nothing to record


@pytest.mark.parametrize("field", ["name", "sku", "category", "unit", "quantity", "reorder_level"])
def test_missing_required_field(app, client, field):
    login(client, "boss")
    response = add(client, **{field: ""})
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert 'class="field-error"' in page and f'id="{field}-error"' in page  # message next to the field
    assert count_products(app) == 2  # only the two fixture products


def test_duplicate_sku_rejected(app, client):
    login(client, "boss")
    response = add(client, sku="t-1")  # fixture already has T-1 (case doesn't matter)
    assert response.status_code == 400
    assert "already used by another product" in response.get_data(as_text=True)
    assert count_products(app) == 2


@pytest.mark.parametrize(
    "field, value",
    [
        ("quantity", "-1"),
        ("quantity", "2.5"),
        ("quantity", "abc"),
        ("reorder_level", "-3"),
        ("reorder_level", "ten"),
        ("unit_cost", "-1.00"),
        ("unit_cost", "abc"),
        ("unit_cost", "1.234"),
        ("unit_cost", "NaN"),
        ("category", "Food"),  # not in the dropdown list
        ("unit", "Gallons"),
        ("sku", "BAD SKU!"),
        ("name", "x" * 121),
        ("description", "x" * 1001),
        ("location", "x" * 101),
        ("supplier", "x" * 121),
    ],
)
def test_invalid_values_rejected(app, client, field, value):
    login(client, "boss")
    response = add(client, **{field: value})
    assert response.status_code == 400
    assert f'id="{field}-error"' in response.get_data(as_text=True)
    assert count_products(app) == 2


def test_form_keeps_values_after_error(client):
    login(client, "boss")
    page = add(client, quantity="-5").get_data(as_text=True)
    assert 'value="Stretch Wrap Film"' in page
    assert 'value="Pacific Packaging Co."' in page
    assert '<option value="Rolls" selected' in page


def test_unit_cost_accepts_dollar_sign(app, client):
    login(client, "boss")
    add(client, unit_cost="$1,250.5")
    with app.app_context():
        assert Product.query.filter_by(sku="SF-2001").one().unit_cost == Decimal("1250.50")


def test_employee_can_create_product(app, client):
    login(client, "alice")
    assert "+ Add product" in client.get("/inventory").get_data(as_text=True)
    form_page = client.get("/products/new").get_data(as_text=True)
    assert form_page and 'name="unit_cost"' not in form_page  # cost field hidden

    # Even if an employee sends a unit cost anyway, it is ignored.
    response = add(client, unit_cost="99.99")
    assert response.status_code == 302
    with app.app_context():
        product = Product.query.filter_by(sku="SF-2001").one()
        assert product.unit_cost is None
        assert product.quantity == 25
        movement = StockMovement.query.filter_by(product_id=product.id).one()
        assert movement.actor.username == "alice" and movement.note == "Starting stock"


def test_employee_invalid_input_still_rejected(app, client):
    login(client, "alice")
    assert add(client, quantity="-1").status_code == 400
    assert add(client, sku="T-1").status_code == 400  # duplicate SKU
    assert count_products(app) == 2


def test_logged_out_user_cannot_create_product(client):
    response = add(client)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# Editing products
# ---------------------------------------------------------------------------

def test_manager_edits_product_without_changing_stock(app, client):
    pid = product_id(app, "T-1")
    login(client, "boss")
    assert client.get(f"/products/{pid}/edit").status_code == 200

    response = client.post(
        f"/products/{pid}/edit",
        data={**VALID_PRODUCT, "sku": "T-1", "name": "Widget Pro", "quantity": "9999"},
    )
    assert response.status_code == 302
    with app.app_context():
        product = db.session.get(Product, pid)
        assert product.name == "Widget Pro"
        assert product.category == "Packaging Supplies"
        assert product.quantity == 10  # quantity is NOT editable here
        assert product.updated_at >= product.created_at


def test_edit_rejects_another_products_sku(app, client):
    login(client, "boss")
    response = client.post(
        f"/products/{product_id(app, 'T-1')}/edit", data={**VALID_PRODUCT, "sku": "T-2"}
    )
    assert response.status_code == 400


def test_employee_cannot_edit_product(app, client):
    pid = product_id(app, "T-1")
    login(client, "alice")
    assert client.get(f"/products/{pid}/edit").status_code == 403
    assert client.post(f"/products/{pid}/edit", data={**VALID_PRODUCT, "sku": "T-1"}).status_code == 403
    with app.app_context():
        assert db.session.get(Product, pid).name == "Widget"


# ---------------------------------------------------------------------------
# Product details page
# ---------------------------------------------------------------------------

def test_details_page_shows_new_fields(app, client):
    login(client, "boss")
    add(client)
    pid = product_id(app, "SF-2001")
    page = client.get(f"/products/{pid}").get_data(as_text=True)
    for text in ["18 in x 1500 ft, clear", "Pacific Packaging Co.", "Warehouse A, Aisle 3",
                 "Packaging Supplies", "$12.50", "Created", "Last updated", "Edit product"]:
        assert text in page


def test_unit_cost_hidden_from_employees(app, client):
    login(client, "boss")
    add(client)
    client.post("/logout")
    login(client, "alice")
    page = client.get(f"/products/{product_id(app, 'SF-2001')}").get_data(as_text=True)
    assert "Pacific Packaging Co." in page  # supplier is visible
    assert "$12.50" not in page and "Unit cost" not in page
    assert "Edit product" not in page and "Add to stock" not in page


def test_inventory_table_shows_new_columns(app, client):
    login(client, "boss")
    add(client)
    page = client.get("/inventory").get_data(as_text=True)
    for text in ["Category", "Unit", "Location", "Packaging Supplies", "Rolls", "Warehouse A, Aisle 3"]:
        assert text in page


def test_product_page_requires_login(app, client):
    response = client.get(f"/products/{product_id(app, 'T-1')}")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_unknown_product_is_404(client):
    login(client, "boss")
    assert client.get("/products/9999").status_code == 404
    assert client.get("/products/9999/edit").status_code == 404
    assert client.post("/products/9999/restock", data={"quantity": "1"}).status_code == 404


def test_product_page_hides_other_employees_reports(app, client):
    pid = product_id(app, "T-1")
    login(client, "bob")
    submit(client, pid, notes="BOB-PRIVATE-NOTE")
    client.post("/logout")

    login(client, "alice")
    assert "BOB-PRIVATE-NOTE" not in client.get(f"/products/{pid}").get_data(as_text=True)
    client.post("/logout")

    login(client, "boss")  # managers see every report
    assert "BOB-PRIVATE-NOTE" in client.get(f"/products/{pid}").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Adding stock (managers only)
# ---------------------------------------------------------------------------

def restock(client, pid, quantity="5", note="Delivery"):
    return client.post(f"/products/{pid}/restock", data={"quantity": quantity, "note": note})


def test_manager_adds_stock_and_it_is_recorded(app, client):
    pid = product_id(app, "T-1")
    login(client, "boss")
    restock(client, pid, quantity="15", note="Supplier delivery")
    with app.app_context():
        assert db.session.get(Product, pid).quantity == 25  # 10 + 15
        movement = StockMovement.query.one()
        assert movement.quantity_change == 15
        assert movement.actor.username == "boss"
        assert movement.note == "Supplier delivery"
    page = client.get(f"/products/{pid}").get_data(as_text=True)
    assert "+15" in page and "Supplier delivery" in page


def test_employee_cannot_add_stock(app, client):
    pid = product_id(app, "T-1")
    login(client, "alice")
    assert restock(client, pid).status_code == 403
    with app.app_context():
        assert db.session.get(Product, pid).quantity == 10
        assert StockMovement.query.count() == 0


@pytest.mark.parametrize("bad_quantity", ["0", "-4", "abc", "", "999999999"])
def test_invalid_restock_quantity_rejected(app, client, bad_quantity):
    pid = product_id(app, "T-1")
    login(client, "boss")
    restock(client, pid, quantity=bad_quantity)
    with app.app_context():
        assert db.session.get(Product, pid).quantity == 10
        assert StockMovement.query.count() == 0


def test_approval_shows_in_stock_history(app, client):
    pid = product_id(app, "T-1")
    login(client, "alice")
    submit(client, pid, quantity=3, reason="Missing")
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/approve")
    page = client.get(f"/products/{pid}").get_data(as_text=True)
    assert "-3" in page and f"Missing (request #{request_id})" in page


# ---------------------------------------------------------------------------
# Inventory status logic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "quantity, reorder_level, expected",
    [
        (0, 5, "Out of Stock"),
        (0, 0, "Out of Stock"),
        (1, 5, "Low Stock"),
        (5, 5, "Low Stock"),   # equal to reorder level counts as low
        (6, 5, "In Stock"),
        (1, 0, "In Stock"),
    ],
)
def test_stock_status_rules(quantity, reorder_level, expected):
    assert Product(quantity=quantity, reorder_level=reorder_level).stock_status == expected


def test_status_updates_when_quantity_changes(app, client):
    pid = product_id(app, "T-2")  # 2 in stock, reorder level 5 -> Low Stock
    login(client, "boss")
    with app.app_context():
        assert db.session.get(Product, pid).stock_status == "Low Stock"
    restock(client, pid, quantity="10")  # 12 > 5
    with app.app_context():
        assert db.session.get(Product, pid).stock_status == "In Stock"
    client.post("/logout")

    login(client, "alice")
    submit(client, pid, quantity=12, reason="Missing")
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/approve")
    with app.app_context():
        assert db.session.get(Product, pid).stock_status == "Out of Stock"
