"""Restock workflow: request -> approve (authorize only) -> receive stock."""

from conftest import login, product_id
from models import (
    ActivityLog,
    InventoryRequest,
    Product,
    StockMovement,
    StockReceipt,
    db,
)
from stock import archive_blocker

# Fixture products: Widget T-1 qty 10 / reorder 3, Gadget T-2 qty 2 / reorder 5.


def request_restock(client, pid, quantity=10, notes="Running low"):
    return client.post("/restock/new", data={"product_id": pid, "quantity": quantity, "notes": notes})


def the_restock(app):
    with app.app_context():
        return InventoryRequest.query.filter_by(request_type="restock").one()


def quantity_of(app, sku):
    with app.app_context():
        return Product.query.filter_by(sku=sku).one().quantity


def employee_restock_then_approve(app, client, quantity=10, sku="T-2"):
    """alice requests a restock, boss approves it. Leaves boss logged in. Returns the request id."""
    login(client, "alice")
    request_restock(client, product_id(app, sku), quantity=quantity)
    client.post("/logout")
    login(client, "boss")
    request_id = the_restock(app).id
    client.post(f"/requests/{request_id}/approve")
    return request_id


# ---------------------------------------------------------------------------
# Creating restock requests
# ---------------------------------------------------------------------------

def test_employee_submits_pending_restock(app, client):
    login(client, "alice")
    response = request_restock(client, product_id(app, "T-2"), quantity=8)
    assert response.headers["Location"].endswith("/employee")
    restock = the_restock(app)
    assert (restock.status, restock.quantity, restock.received_quantity) == ("Pending", 8, 0)
    assert quantity_of(app, "T-2") == 2  # nothing changes yet
    with app.app_context():
        assert ActivityLog.query.filter(ActivityLog.action.like("Submitted restock request%")).count() == 1
    # The employee can follow its status
    assert "Restock 8" in client.get("/employee").get_data(as_text=True)


def test_restock_validation(app, client):
    with app.app_context():
        db.session.add(Product(sku="T-9", name="Retired", quantity=0, reorder_level=1, is_archived=True))
        db.session.commit()
    login(client, "alice")
    assert request_restock(client, product_id(app, "T-2"), quantity=0).status_code == 400
    assert request_restock(client, product_id(app, "T-2"), quantity="abc").status_code == 400
    assert request_restock(client, product_id(app, "T-2"), notes="  ").status_code == 400
    assert request_restock(client, product_id(app, "T-9")).status_code == 400  # archived
    assert request_restock(client, 99999).status_code == 400
    with app.app_context():
        assert InventoryRequest.query.count() == 0


def test_form_suggests_quantity_from_target(app, client):
    pid = product_id(app, "T-2")
    with app.app_context():
        db.session.get(Product, pid).target_stock_level = 12
        db.session.commit()
    login(client, "alice")
    page = client.get(f"/restock/new?product_id={pid}").get_data(as_text=True)
    assert 'value="10"' in page  # 12 target - 2 in stock


def test_manager_restock_is_authorized_immediately(app, client):
    login(client, "boss")
    response = request_restock(client, product_id(app, "T-2"), quantity=5)
    assert response.headers["Location"].endswith("/restock")
    restock = the_restock(app)
    assert restock.status == "Approved"
    assert quantity_of(app, "T-2") == 2


# ---------------------------------------------------------------------------
# Approval authorizes but does not change inventory
# ---------------------------------------------------------------------------

def test_approval_does_not_change_inventory(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    assert the_restock(app).status == "Approved"
    assert quantity_of(app, "T-2") == 2
    with app.app_context():
        assert StockMovement.query.count() == 0
    page = client.get("/restock").get_data(as_text=True)
    assert f">{request_id}</a>" in page and "Receive stock" in page


def test_rejecting_restock_needs_note(app, client):
    login(client, "alice")
    request_restock(client, product_id(app, "T-2"))
    client.post("/logout")
    login(client, "boss")
    request_id = the_restock(app).id
    client.post(f"/requests/{request_id}/reject")
    assert the_restock(app).status == "Pending"
    client.post(f"/requests/{request_id}/reject", data={"manager_note": "Supplier discontinued it"})
    assert the_restock(app).status == "Rejected"
    assert quantity_of(app, "T-2") == 2


def test_restock_type_filter_on_approvals(app, client):
    login(client, "alice")
    request_restock(client, product_id(app, "T-2"), notes="restock-note")
    client.post("/requests", data={"product_id": product_id(app, "T-1"), "quantity": 1,
                                   "reason": "Damaged", "notes": "damage-note"})
    client.post("/logout")
    login(client, "boss")
    page = client.get("/approvals?type=restock").get_data(as_text=True)
    assert "restock-note" in page and "damage-note" not in page


# ---------------------------------------------------------------------------
# Receiving stock
# ---------------------------------------------------------------------------

def test_partial_then_full_receipt(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)

    client.post(f"/restock/{request_id}/receive", data={"quantity": 6, "note": "First pallet"})
    restock = the_restock(app)
    assert (restock.status, restock.received_quantity) == ("Partially Received", 6)
    assert quantity_of(app, "T-2") == 8

    client.post(f"/restock/{request_id}/receive", data={"quantity": 4})
    restock = the_restock(app)
    assert (restock.status, restock.received_quantity) == ("Received", 10)
    assert quantity_of(app, "T-2") == 12

    with app.app_context():
        receipts = StockReceipt.query.order_by(StockReceipt.id).all()
        assert [r.quantity for r in receipts] == [6, 4]
        assert receipts[0].note == "First pallet"
        # Each receipt is linked to the stock movement it created.
        assert [r.movement.quantity_change for r in receipts] == [6, 4]
        assert all(f"request #{request_id}" in r.movement.note for r in receipts)
        assert ActivityLog.query.filter(ActivityLog.action.like(f"Received % for restock #{request_id}%")).count() == 2

    detail = client.get(f"/requests/{request_id}").get_data(as_text=True)
    assert "Deliveries" in detail and "+6" in detail and "+4" in detail


def test_cannot_receive_more_than_remaining(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    client.post(f"/restock/{request_id}/receive", data={"quantity": 6})
    response = client.post(f"/restock/{request_id}/receive", data={"quantity": 5}, follow_redirects=True)
    assert "Only 4 unit(s)" in response.get_data(as_text=True)
    assert the_restock(app).received_quantity == 6
    assert quantity_of(app, "T-2") == 8


def test_cannot_receive_after_fully_received(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=3)
    client.post(f"/restock/{request_id}/receive", data={"quantity": 3})
    client.post(f"/restock/{request_id}/receive", data={"quantity": 1})
    assert quantity_of(app, "T-2") == 5
    with app.app_context():
        assert StockReceipt.query.count() == 1


def test_cannot_receive_pending_or_rejected_restock(app, client):
    login(client, "alice")
    request_restock(client, product_id(app, "T-2"))
    client.post("/logout")
    login(client, "boss")
    request_id = the_restock(app).id
    client.post(f"/restock/{request_id}/receive", data={"quantity": 1})  # still Pending
    client.post(f"/requests/{request_id}/reject", data={"manager_note": "No"})
    client.post(f"/restock/{request_id}/receive", data={"quantity": 1})  # Rejected
    assert quantity_of(app, "T-2") == 2
    with app.app_context():
        assert StockReceipt.query.count() == 0


def test_cannot_receive_into_non_restock_request(app, client):
    login(client, "alice")
    client.post("/requests", data={"product_id": product_id(app, "T-1"), "quantity": 1,
                                   "reason": "Damaged", "notes": "x"})
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/restock/{request_id}/receive", data={"quantity": 5})
    assert quantity_of(app, "T-1") == 9  # only the approved -1


def test_invalid_received_quantities(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    for bad in ["0", "-2", "abc", "1.5", ""]:
        client.post(f"/restock/{request_id}/receive", data={"quantity": bad})
    assert the_restock(app).received_quantity == 0
    assert quantity_of(app, "T-2") == 2


def test_employees_cannot_receive_or_see_orders(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    client.post("/logout")
    login(client, "alice")
    assert client.post(f"/restock/{request_id}/receive", data={"quantity": 5}).status_code == 403
    assert client.post(f"/restock/{request_id}/close", data={"note": "x"}).status_code == 403
    assert client.get("/restock").status_code == 403
    assert client.get("/restock/new").status_code == 200  # but they can request one
    assert quantity_of(app, "T-2") == 2


def test_close_order_without_changing_stock(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    client.post(f"/restock/{request_id}/receive", data={"quantity": 4})
    client.post(f"/restock/{request_id}/close", data={"note": ""})  # note required
    assert the_restock(app).status == "Partially Received"
    client.post(f"/restock/{request_id}/close", data={"note": "Supplier out of stock"})
    restock = the_restock(app)
    assert restock.status == "Received"
    assert "Closed with 4 of 10 received" in restock.manager_note
    assert quantity_of(app, "T-2") == 6
    client.post(f"/restock/{request_id}/receive", data={"quantity": 1})  # no longer open
    assert quantity_of(app, "T-2") == 6


# ---------------------------------------------------------------------------
# Effects elsewhere
# ---------------------------------------------------------------------------

def test_open_restock_blocks_archiving(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    pid = product_id(app, "T-2")
    with app.app_context():
        assert "open restock" in archive_blocker(db.session.get(Product, pid))
    client.post(f"/restock/{request_id}/receive", data={"quantity": 10})
    with app.app_context():
        assert archive_blocker(db.session.get(Product, pid)) is None


def test_on_order_shown_on_stock_alerts(app, client):
    request_id = employee_restock_then_approve(app, client, quantity=10)
    client.post(f"/restock/{request_id}/receive", data={"quantity": 3})
    page = client.get("/alerts").get_data(as_text=True)
    table = page.split("<tbody>")[1]
    gadget_row = table.split("Gadget")[1].split("</tr>")[0]
    assert '<td class="num">7</td>' in gadget_row  # 10 ordered - 3 received
    assert "Create restock request" in gadget_row
