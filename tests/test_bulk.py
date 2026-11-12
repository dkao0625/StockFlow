"""
Bulk editing, bulk stock adjustment, bulk delete (archive), and the manager
review of those requests. Numbers in comments match the scenarios in the
assignment brief.
"""

import pytest

from conftest import login, product_id, submit
from models import ActivityLog, InventoryRequest, Product, StockMovement, db


@pytest.fixture
def five_products(app):
    """Five extra products (Electronics) on top of the two fixture products."""
    with app.app_context():
        products = [
            Product(sku=f"E-{n}", name=f"Gadget {n}", category="Electronics", unit="Pieces",
                    quantity=20, reorder_level=5, location="Aisle 1", supplier="Old Supplier")
            for n in range(1, 6)
        ]
        db.session.add_all(products)
        db.session.commit()
        return [p.id for p in products]


def get(app, pid):
    with app.app_context():
        product = db.session.get(Product, pid)
        db.session.expunge(product)
        return product


def pending_requests(app):
    with app.app_context():
        return InventoryRequest.query.filter_by(status="Pending").order_by(InventoryRequest.id).all()


def adjust_form(ids, direction="decrease", quantity="2", reason="Damaged", note="Dropped"):
    data = {"product_ids": ids}
    for pid in ids:
        data.update({f"direction_{pid}": direction, f"quantity_{pid}": quantity,
                     f"reason_{pid}": reason, f"note_{pid}": note})
    return data


# ---------------------------------------------------------------------------
# Inventory page: filters, edit mode, Select All
# ---------------------------------------------------------------------------

def test_inventory_page_has_edit_mode_and_toolbar(client, five_products):
    login(client, "alice")
    page = client.get("/inventory").get_data(as_text=True)
    for text in ["Edit Inventory", "data-select-all", "Bulk Edit", "Adjust Stock", "Delete", "data-bulk-cancel"]:
        assert text in page
    assert page.count('name="product_ids"') == 7  # one checkbox per product


def test_select_all_only_covers_filtered_products(app, client, five_products):
    """4. Select All ticks the visible rows, and only matching rows are sent."""
    login(client, "alice")
    page = client.get("/inventory?category=Electronics").get_data(as_text=True)
    assert page.count('name="product_ids"') == 5
    assert f'value="{product_id(app, "T-1")}" data-select-item' not in page  # Widget (Other) hidden

    page = client.get("/inventory?q=gadget 3").get_data(as_text=True)
    assert page.count('name="product_ids"') == 1

    page = client.get("/inventory?status=Low Stock").get_data(as_text=True)
    assert page.count('name="product_ids"') == 1  # only T-2 (2 left, reorder at 5)


# ---------------------------------------------------------------------------
# Bulk edit
# ---------------------------------------------------------------------------

def test_employee_bulk_updates_location_only(app, client, five_products):
    """2 + 3. Five products get the new location; nothing else changes."""
    untouched_before = get(app, product_id(app, "T-1"))
    login(client, "alice")

    page = client.get("/bulk/edit", query_string={"product_ids": five_products}).get_data(as_text=True)
    assert "5 products will be affected" in page
    assert 'name="update_reorder_level"' not in page  # manager-only field not offered

    response = client.post("/bulk/edit", data={
        "product_ids": five_products,
        "update_location": "1", "location": "Warehouse B",
        # Values typed into fields that are NOT switched on must be ignored:
        "category": "Clothing", "supplier": "Should Not Save",
    })
    assert response.status_code == 302

    with app.app_context():
        for pid in five_products:
            product = db.session.get(Product, pid)
            assert product.location == "Warehouse B"
            assert product.category == "Electronics" and product.supplier == "Old Supplier"
            assert product.quantity == 20 and product.name.startswith("Gadget")
        # Who changed what is recorded for every product
        logs = ActivityLog.query.filter(ActivityLog.action.like("Bulk edited%")).all()
        assert len(logs) == 5
        assert all(log.actor.username == "alice" and "location" in log.action for log in logs)

    # 3. Products that weren't selected are exactly as before
    untouched_after = get(app, product_id(app, "T-1"))
    assert untouched_after.location == untouched_before.location
    assert untouched_after.updated_at == untouched_before.updated_at


def test_bulk_edit_requires_a_switched_on_field(app, client, five_products):
    login(client, "alice")
    response = client.post("/bulk/edit", data={"product_ids": five_products, "location": "X"})
    assert response.status_code == 400
    assert get(app, five_products[0]).location == "Aisle 1"


def test_bulk_edit_rejects_invalid_values(app, client, five_products):
    login(client, "alice")
    response = client.post("/bulk/edit", data={
        "product_ids": five_products, "update_category": "1", "category": "Food",
    })
    assert response.status_code == 400
    assert get(app, five_products[0]).category == "Electronics"


def test_manager_can_bulk_update_reorder_level(app, client, five_products):
    login(client, "boss")
    client.post("/bulk/edit", data={
        "product_ids": five_products[:2], "update_reorder_level": "1", "reorder_level": "50",
    })
    assert get(app, five_products[0]).reorder_level == 50
    assert get(app, five_products[0]).stock_status == "Low Stock"  # 20 <= 50
    assert get(app, five_products[2]).reorder_level == 5


# ---------------------------------------------------------------------------
# Bulk stock adjustment
# ---------------------------------------------------------------------------

def test_employee_bulk_adjustment_creates_pending_requests(app, client, five_products):
    """5 + 7. Employees request changes; stock does not move yet."""
    login(client, "alice")
    ids = five_products[:3]
    data = adjust_form(ids)
    data.update({f"direction_{ids[2]}": "increase", f"reason_{ids[2]}": "Received", f"quantity_{ids[2]}": "10"})
    response = client.post("/bulk/adjust", data=data)
    assert response.status_code == 302

    requests_ = pending_requests(app)
    assert len(requests_) == 3
    assert [r.signed_quantity for r in requests_] == [-2, -2, 10]
    assert all(r.employee_id and r.request_type == "adjustment" for r in requests_)
    for pid in ids:
        assert get(app, pid).quantity == 20  # unchanged until a manager approves
    with app.app_context():
        assert StockMovement.query.count() == 0


def test_manager_approves_bulk_adjustment(app, client, five_products):
    """8. Approving updates stock and writes stock movements."""
    login(client, "alice")
    ids = five_products[:2]
    data = adjust_form(ids, quantity="3")
    data.update({f"direction_{ids[1]}": "increase", f"reason_{ids[1]}": "Found", f"quantity_{ids[1]}": "7"})
    client.post("/bulk/adjust", data=data)
    client.post("/logout")

    login(client, "boss")
    request_ids = [r.id for r in pending_requests(app)]
    response = client.post("/requests/bulk-decision", data={"request_ids": request_ids, "decision": "approve"})
    assert response.status_code == 302
    assert get(app, ids[0]).quantity == 17   # 20 - 3
    assert get(app, ids[1]).quantity == 27   # 20 + 7
    with app.app_context():
        changes = sorted(m.quantity_change for m in StockMovement.query.all())
        assert changes == [-3, 7]
        assert all(r.status == "Approved" for r in InventoryRequest.query.all())


def test_bulk_adjustment_rejects_bad_rows_and_saves_nothing(app, client, five_products):
    login(client, "alice")
    ids = five_products[:2]
    data = adjust_form(ids)
    data[f"quantity_{ids[1]}"] = "21"  # more than the 20 in stock
    response = client.post("/bulk/adjust", data=data)
    assert response.status_code == 400
    assert "Only 20 in stock" in response.get_data(as_text=True)
    assert pending_requests(app) == []  # the valid row wasn't saved either

    data = adjust_form(ids, direction="increase", reason="Damaged")  # reason doesn't fit
    assert client.post("/bulk/adjust", data=data).status_code == 400
    assert pending_requests(app) == []


def test_manager_bulk_adjustment_applies_directly(app, client, five_products):
    login(client, "boss")
    client.post("/bulk/adjust", data=adjust_form(five_products[:2], direction="increase", reason="Received", quantity="5"))
    assert get(app, five_products[0]).quantity == 25
    with app.app_context():
        assert StockMovement.query.count() == 2
        assert InventoryRequest.query.count() == 0


def test_bulk_approval_is_all_or_nothing(app, client):
    """If one selected request can't be approved, none of them are."""
    widget, gadget = product_id(app, "T-1"), product_id(app, "T-2")
    login(client, "alice")
    submit(client, widget, quantity=1)
    submit(client, gadget, quantity=2)
    submit(client, gadget, quantity=2)  # together with the one above: 4 > 2 in stock
    client.post("/logout")

    login(client, "boss")
    request_ids = [r.id for r in pending_requests(app)]
    response = client.post("/requests/bulk-decision", data={"request_ids": request_ids, "decision": "approve"},
                           follow_redirects=True)
    assert "Nothing was approved" in response.get_data(as_text=True)
    assert get(app, widget).quantity == 10 and get(app, gadget).quantity == 2
    assert len(pending_requests(app)) == 3


def test_bulk_reject_needs_note_and_changes_nothing(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)
    client.post("/logout")
    login(client, "boss")
    ids = [r.id for r in pending_requests(app)]
    client.post("/requests/bulk-decision", data={"request_ids": ids, "decision": "reject"})
    assert len(pending_requests(app)) == 1  # note was missing
    client.post("/requests/bulk-decision", data={"request_ids": ids, "decision": "reject", "manager_note": "No"})
    assert pending_requests(app) == []
    assert get(app, product_id(app, "T-1")).quantity == 10


# ---------------------------------------------------------------------------
# Bulk delete (archive)
# ---------------------------------------------------------------------------

def test_employee_deletion_request_then_manager_approves(app, client, five_products):
    """6 + 9 + 10 + 11."""
    ids = five_products[:3]
    login(client, "alice")
    page = client.get("/bulk/archive", query_string={"product_ids": ids}).get_data(as_text=True)
    assert "Are you sure you want to request deletion of 3 selected products?" in page

    client.post("/bulk/archive", data={"product_ids": ids, "note": "Discontinued"})
    # 6. The employee could NOT archive anything directly
    assert not any(get(app, pid).is_archived for pid in ids)
    requests_ = pending_requests(app)
    assert len(requests_) == 3 and all(r.request_type == "archive" for r in requests_)
    client.post("/logout")

    # 10. Manager approves -> products archived
    login(client, "boss")
    client.post("/requests/bulk-decision",
                data={"request_ids": [r.id for r in requests_], "decision": "approve"})
    for pid in ids:
        product = get(app, pid)
        assert product.is_archived and product.archived_by_id is not None
    assert not get(app, five_products[3]).is_archived  # unselected product untouched

    # Hidden from active inventory, listed under archived
    active_page = client.get("/inventory").get_data(as_text=True)
    assert "Gadget 1" not in active_page and "Gadget 4" in active_page
    assert "Gadget 1" in client.get("/inventory?show=archived").get_data(as_text=True)

    # 11. History is still there and the product page still works
    with app.app_context():
        assert InventoryRequest.query.filter_by(product_id=ids[0]).count() == 1
        assert db.session.get(Product, ids[0]) is not None
    detail = client.get(f"/products/{ids[0]}")
    assert detail.status_code == 200 and "Archived" in detail.get_data(as_text=True)
    assert "Gadget 1" in client.get("/manager").get_data(as_text=True)  # in request history


def test_manager_archives_directly_with_confirmation(app, client, five_products):
    ids = five_products[:2]
    login(client, "boss")
    page = client.get("/bulk/archive", query_string={"product_ids": ids}).get_data(as_text=True)
    assert "Archive 2 products?" in page and 'name="acknowledge"' in page

    client.post("/bulk/archive", data={"product_ids": ids})  # no acknowledgement
    assert not get(app, ids[0]).is_archived

    client.post("/bulk/archive", data={"product_ids": ids, "acknowledge": "yes"})
    assert get(app, ids[0]).is_archived and get(app, ids[1]).is_archived


def test_archive_blocked_by_pending_requests(app, client):
    widget = product_id(app, "T-1")
    login(client, "alice")
    submit(client, widget, quantity=1)  # pending stock request
    page = client.get("/bulk/archive", query_string={"product_ids": [widget]}).get_data(as_text=True)
    assert "pending stock request" in page
    client.post("/bulk/archive", data={"product_ids": [widget]})
    assert [r.request_type for r in pending_requests(app)] == ["adjustment"]  # no archive request made
    client.post("/logout")

    login(client, "boss")
    client.post("/bulk/archive", data={"product_ids": [widget], "acknowledge": "yes"})
    assert not get(app, widget).is_archived


def test_archived_products_cannot_be_changed(app, client, five_products):
    pid = five_products[0]
    login(client, "boss")
    client.post("/bulk/archive", data={"product_ids": [pid], "acknowledge": "yes"})

    client.post(f"/products/{pid}/restock", data={"quantity": "5"})
    client.post("/bulk/edit", data={"product_ids": [pid], "update_location": "1", "location": "Z"})
    product = get(app, pid)
    assert product.quantity == 20 and product.location == "Aisle 1"


# ---------------------------------------------------------------------------
# 12. Permissions can't be bypassed with hand-crafted requests
# ---------------------------------------------------------------------------

def test_employee_cannot_bypass_permissions(app, client, five_products):
    pid = five_products[0]
    login(client, "alice")
    # Manager-only bulk field
    assert client.post("/bulk/edit", data={
        "product_ids": [pid], "update_reorder_level": "1", "reorder_level": "1"}).status_code == 403
    # Direct stock change, manager review, activity history
    assert client.post(f"/products/{pid}/restock", data={"quantity": "5"}).status_code == 403
    assert client.post("/requests/bulk-decision", data={"request_ids": [1], "decision": "approve"}).status_code == 403
    assert client.get("/activity").status_code == 403
    # "acknowledge" doesn't let an employee archive directly
    client.post("/bulk/archive", data={"product_ids": [pid], "acknowledge": "yes"})
    assert not get(app, pid).is_archived
    assert get(app, pid).reorder_level == 5 and get(app, pid).quantity == 20


def test_logged_out_user_is_sent_to_login(client, five_products):
    for url in ["/bulk/edit", "/bulk/adjust", "/bulk/archive"]:
        response = client.post(url, data={"product_ids": five_products})
        assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_invalid_product_ids_are_rejected(app, client, five_products):
    login(client, "alice")
    assert client.get("/bulk/edit?product_ids=abc").status_code == 400
    response = client.post("/bulk/edit", data={
        "product_ids": [five_products[0], 99999], "update_location": "1", "location": "Nowhere"})
    assert response.status_code == 302
    assert get(app, five_products[0]).location == "Aisle 1"  # nothing saved


def test_activity_page_for_managers(app, client, five_products):
    login(client, "alice")
    client.post("/bulk/edit", data={"product_ids": five_products[:1], "update_supplier": "1", "supplier": "Acme"})
    client.post("/logout")
    login(client, "boss")
    page = client.get("/activity").get_data(as_text=True)
    assert "Bulk edited E-1" in page and "alice" in page
    assert "Acme" in client.get(f"/products/{five_products[0]}").get_data(as_text=True)
