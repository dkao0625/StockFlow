"""Adding products from the inventory page (managers only)."""

import pytest

from conftest import login
from models import ActivityLog, Product, StockMovement


def add(client, name="Stretch Wrap", sku="sf-2001", quantity="25", reorder_level="5"):
    return client.post(
        "/products",
        data={"name": name, "sku": sku, "quantity": quantity, "reorder_level": reorder_level},
    )


def test_manager_can_add_product(app, client):
    login(client, "boss")
    response = add(client)
    assert response.status_code == 302

    with app.app_context():
        product = Product.query.filter_by(sku="SF-2001").one()  # SKU stored uppercase
        assert product.name == "Stretch Wrap"
        assert product.quantity == 25
        assert product.reorder_level == 5
        movement = StockMovement.query.filter_by(product_id=product.id).one()
        assert movement.quantity_change == 25 and movement.request_id is None
        assert ActivityLog.query.filter(ActivityLog.action.like("Added product SF-2001%")).count() == 1

    page = client.get("/inventory").get_data(as_text=True)
    assert "Stretch Wrap" in page and "Add a product" in page


def test_zero_starting_stock_is_allowed(app, client):
    login(client, "boss")
    add(client, sku="SF-2002", quantity="0")
    with app.app_context():
        product = Product.query.filter_by(sku="SF-2002").one()
        assert product.stock_status == "Out of Stock"
        assert StockMovement.query.count() == 0  # nothing to record


def test_employee_cannot_add_product(app, client):
    login(client, "alice")
    assert add(client).status_code == 403
    assert "Add a product" not in client.get("/inventory").get_data(as_text=True)
    with app.app_context():
        assert Product.query.count() == 2  # only the fixture products


def test_logged_out_user_cannot_add_product(app, client):
    response = add(client)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_duplicate_sku_rejected(app, client):
    login(client, "boss")
    response = add(client, sku="t-1")  # fixture already has T-1
    assert response.status_code == 400
    assert "already exists" in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", ""),
        ("name", "x" * 121),
        ("sku", ""),
        ("sku", "BAD SKU!"),
        ("quantity", "-1"),
        ("quantity", "abc"),
        ("quantity", "1.5"),
        ("reorder_level", "-3"),
        ("reorder_level", ""),
    ],
)
def test_invalid_product_input_rejected(app, client, field, value):
    login(client, "boss")
    values = {"name": "Thing", "sku": "SF-3000", "quantity": "5", "reorder_level": "1"}
    values[field] = value
    response = add(client, **values)
    assert response.status_code == 400
    # The form keeps what the manager typed so they can fix it.
    assert 'value="Thing"' in response.get_data(as_text=True) or field == "name"
    with app.app_context():
        assert Product.query.count() == 2
