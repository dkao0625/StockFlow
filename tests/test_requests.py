"""Inventory request workflow tests: validation, approval, rejection."""

import pytest

from conftest import login, product_id, submit
from models import InventoryRequest, Product, StockMovement, db


def get_product(app, sku):
    with app.app_context():
        product = Product.query.filter_by(sku=sku).one()
        return product.quantity


def only_request(app):
    with app.app_context():
        return InventoryRequest.query.one()


@pytest.mark.parametrize("bad_quantity", ["0", "-5", "abc", "2.5", "", "999999999"])
def test_invalid_quantities_are_rejected(app, client, bad_quantity):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=bad_quantity)
    with app.app_context():
        assert InventoryRequest.query.count() == 0


def test_cannot_report_more_than_in_stock(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-2"), quantity=3)  # only 2 in stock
    with app.app_context():
        assert InventoryRequest.query.count() == 0


def test_invalid_reason_and_missing_notes_rejected(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), reason="Stolen")
    submit(client, product_id(app, "T-1"), notes="   ")
    with app.app_context():
        assert InventoryRequest.query.count() == 0


def test_full_workflow_approve_subtracts_stock(app, client):
    # Employee submits a report -> Pending
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=3)
    assert only_request(app).status == "Pending"
    assert get_product(app, "T-1") == 10  # no change until approved
    client.post("/logout")

    # Manager approves -> stock goes from 10 to 7
    login(client, "boss")
    request_id = only_request(app).id
    client.post(f"/requests/{request_id}/approve", data={"manager_note": "Confirmed"})
    assert get_product(app, "T-1") == 7

    with app.app_context():
        req = db.session.get(InventoryRequest, request_id)
        assert req.status == "Approved"
        assert req.manager.username == "boss"
        movement = StockMovement.query.one()
        assert movement.quantity_change == -3
        assert movement.request_id == request_id
    client.post("/logout")

    # Employee sees the new status
    login(client, "alice")
    page = client.get("/employee").get_data(as_text=True)
    assert "Approved" in page and "Confirmed" in page


def test_approving_twice_only_subtracts_once(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=4)
    client.post("/logout")

    login(client, "boss")
    request_id = only_request(app).id
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/requests/{request_id}/approve")
    assert get_product(app, "T-1") == 6
    with app.app_context():
        assert StockMovement.query.count() == 1


def test_reject_does_not_change_stock(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=5)
    client.post("/logout")

    login(client, "boss")
    request_id = only_request(app).id
    # A note is required to reject
    client.post(f"/requests/{request_id}/reject", data={"manager_note": ""})
    assert only_request(app).status == "Pending"

    client.post(f"/requests/{request_id}/reject", data={"manager_note": "Not damaged"})
    assert only_request(app).status == "Rejected"
    assert get_product(app, "T-1") == 10
    with app.app_context():
        assert StockMovement.query.count() == 0

    # A rejected request can't be approved afterwards
    client.post(f"/requests/{request_id}/approve")
    assert only_request(app).status == "Rejected"
    assert get_product(app, "T-1") == 10


def test_approval_blocked_when_stock_dropped(app, client):
    # Two employees each report 2 of the 2 Gadgets in stock.
    pid = product_id(app, "T-2")
    login(client, "alice")
    submit(client, pid, quantity=2)
    client.post("/logout")
    login(client, "bob")
    submit(client, pid, quantity=2)
    client.post("/logout")

    login(client, "boss")
    with app.app_context():
        first_id, second_id = [r.id for r in InventoryRequest.query.order_by(InventoryRequest.id)]
    client.post(f"/requests/{first_id}/approve")
    response = client.post(f"/requests/{second_id}/approve", follow_redirects=True)

    assert "Cannot approve" in response.get_data(as_text=True)
    assert get_product(app, "T-2") == 0  # never negative
    with app.app_context():
        assert db.session.get(InventoryRequest, second_id).status == "Pending"


def test_unknown_request_returns_404(client):
    login(client, "boss")
    assert client.post("/requests/9999/approve").status_code == 404
