"""Employee permissions across the app, Stock Alerts as an employee, and My Activity."""

import pytest

from conftest import login, product_id, submit
from models import ActivityLog, InventoryRequest, Product, StockMovement, User, db

# Fixture products: Widget T-1 qty 10 / reorder 3, Gadget T-2 qty 2 / reorder 5.


def employee_actions(app, username, category):
    with app.app_context():
        return ActivityLog.query.filter_by(
            actor_id=User.query.filter_by(username=username).one().id, category=category
        ).all()


# ---------------------------------------------------------------------------
# Inventory: what employees can do
# ---------------------------------------------------------------------------

def test_employee_can_search_filter_and_view_products(app, client):
    login(client, "alice")
    assert "Gadget" not in client.get("/inventory?q=widget").get_data(as_text=True).split("<tbody>")[1]
    assert "Gadget" in client.get("/inventory?status=Low Stock").get_data(as_text=True)
    assert client.get(f"/products/{product_id(app, 'T-1')}").status_code == 200


def test_employee_edit_is_recorded_with_before_and_after(app, client):
    login(client, "alice")
    widget = product_id(app, "T-1")
    client.post(f"/products/{widget}/edit", data={
        "name": "Widget", "sku": "T-1", "category": "Other", "unit": "Pieces", "location": "Aisle 7",
    })
    [entry] = employee_actions(app, "alice", "product_edited")
    assert entry.actor_role == "employee" and "Aisle 7" in entry.action


def test_employee_bulk_edit_is_recorded_per_product(app, client):
    login(client, "alice")
    client.post("/bulk/edit", data={
        "product_ids": [product_id(app, "T-1"), product_id(app, "T-2")],
        "update_location": "1", "location": "Dock 2",
    })
    entries = employee_actions(app, "alice", "bulk_edit")
    assert len(entries) == 2 and all(e.actor_role == "employee" for e in entries)


def test_employee_bulk_requests_never_change_stock(app, client):
    login(client, "alice")
    widget, gadget = product_id(app, "T-1"), product_id(app, "T-2")
    client.post("/bulk/adjust", data={
        "product_ids": [widget], f"direction_{widget}": "decrease", f"reason_{widget}": "Damaged",
        f"quantity_{widget}": "1", f"note_{widget}": "dropped",
    })
    client.post("/bulk/archive", data={"product_ids": [gadget], "note": "old"})
    with app.app_context():
        assert {r.request_type for r in InventoryRequest.query.filter_by(status="Pending")} == {"adjustment", "archive"}
        assert [p.quantity for p in Product.query.order_by(Product.sku)] == [10, 2]
        assert Product.query.filter_by(is_archived=True).count() == 0
        assert StockMovement.query.count() == 0


# ---------------------------------------------------------------------------
# Inventory: what employees can NOT do (sent directly, bypassing the UI)
# ---------------------------------------------------------------------------

def test_employee_cannot_set_quantity_or_manager_fields(app, client):
    login(client, "alice")
    widget = product_id(app, "T-1")
    base = {"name": "Widget", "sku": "T-1", "category": "Other", "unit": "Pieces"}
    for forbidden in ({"quantity": "999"}, {"reorder_level": "50"}, {"unit_cost": "1.00"}):
        assert client.post(f"/products/{widget}/edit", data=base | forbidden).status_code == 403
    assert client.post(f"/products/{widget}/restock", data={"quantity": "5"}).status_code == 403
    with app.app_context():
        widget_row = db.session.get(Product, widget)
        assert (widget_row.quantity, widget_row.reorder_level, widget_row.unit_cost) == (10, 3, None)


@pytest.mark.parametrize("method,url", [
    ("get", "/overview"), ("get", "/approvals"), ("get", "/manager"), ("get", "/restock"),
    ("get", "/activity"), ("get", "/employees"), ("get", "/reports"),
    ("get", "/inventory/export.csv"), ("get", "/alerts/export.csv"), ("get", "/approvals/export.csv"),
    ("get", "/activity/export.csv"), ("get", "/reports/movements.csv"), ("get", "/reports/requests.csv"),
    ("post", "/requests/{rid}/approve"), ("post", "/requests/{rid}/reject"),
    ("post", "/requests/{rid}/request-correction"), ("post", "/requests/bulk-decision"),
    ("post", "/restock/{rid}/receive"), ("post", "/restock/{rid}/close"),
    ("post", "/alerts/{pid}/levels"), ("post", "/products/{pid}/restock"),
    ("post", "/employees/{uid}/deactivate"), ("post", "/employees/{uid}/activate"),
])
def test_employee_blocked_from_manager_routes(app, client, method, url):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)  # alice's own request
    with app.app_context():
        rid = InventoryRequest.query.one().id
        uid = User.query.filter_by(username="bob").one().id
    url = url.format(rid=rid, pid=product_id(app, "T-1"), uid=uid)
    data = {"manager_note": "x", "quantity": "1", "note": "x", "reorder_level": "1",
            "request_ids": [rid], "decision": "approve"}
    response = client.get(url) if method == "get" else client.post(url, data=data)
    assert response.status_code == 403
    with app.app_context():
        assert InventoryRequest.query.one().status == "Pending"  # can't approve own request
        assert Product.query.filter_by(sku="T-1").one().quantity == 10


# ---------------------------------------------------------------------------
# Stock Alerts as an employee
# ---------------------------------------------------------------------------

def test_employee_stock_alerts(app, client):
    login(client, "alice")
    page = client.get("/alerts?q=gad").get_data(as_text=True)
    rows = page.split("<tbody>")[1]
    gadget = product_id(app, "T-2")
    assert "Gadget" in rows and "Widget" not in rows
    assert f'href="/restock/new?product_id={gadget}">Request restock</a>' in rows
    assert "Set levels" not in page and 'href="/restock"' not in page  # manager-only tools hidden
    form = client.get(f"/restock/new?product_id={gadget}").get_data(as_text=True)
    assert f'value="{gadget}" selected' in " ".join(form.split())  # product pre-selected


# ---------------------------------------------------------------------------
# My Activity
# ---------------------------------------------------------------------------

def test_my_activity_shows_related_request_and_status(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=2, notes="crushed")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
        entry = ActivityLog.query.filter_by(category="damage_report").one()
        assert entry.request_id == request_id
    page = client.get("/my-activity").get_data(as_text=True)
    assert f'href="/requests/{request_id}">#{request_id}</a>' in page
    assert "Pending" in page.split("<tbody>")[1]
    assert "Logged in" not in page  # logins aren't inventory work

    client.post("/logout")
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    client.post("/logout")
    login(client, "alice")
    rows = client.get("/my-activity").get_data(as_text=True).split("<tbody>")[1]
    assert "Approved" in rows  # the status shown is the request's CURRENT status


def test_request_links_are_recorded_through_the_whole_workflow(app, client):
    login(client, "alice")
    client.post("/restock/new", data={"product_id": product_id(app, "T-2"), "quantity": "5", "notes": "Low"})
    client.post("/logout")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/restock/{request_id}/receive", data={"quantity": "5"})
    with app.app_context():
        linked = {e.category for e in ActivityLog.query.filter_by(request_id=request_id)}
    assert linked == {"request_submitted", "request_approved", "stock_received", "stock_increase"}


def test_my_activity_filters_hide_login_category(app, client):
    login(client, "alice")
    page = client.get("/my-activity").get_data(as_text=True)
    assert 'value="login"' not in page and 'value="account"' not in page
    assert client.get("/my-activity?category=login").status_code == 200  # ignored, not an error


def test_manager_log_still_shows_everything_with_request_links(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)
    client.post("/logout")
    login(client, "boss")
    page = client.get("/activity").get_data(as_text=True)
    assert "Logged in" in page and "Related request" in page
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    assert f'href="/requests/{request_id}">#{request_id}</a>' in page
