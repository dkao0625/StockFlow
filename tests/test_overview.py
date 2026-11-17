"""Manager Overview dashboard: access control, real statistics, and quick actions."""

from datetime import timedelta
from decimal import Decimal

from conftest import login, product_id, submit
from models import (
    ActivityLog,
    InventoryRequest,
    Product,
    User,
    db,
    log_activity,
    utc_now,
)


def page(client):
    response = client.get("/overview")
    assert response.status_code == 200
    return response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def test_overview_requires_login(client):
    response = client.get("/overview")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_employee_cannot_see_overview(client):
    login(client, "alice")
    assert client.get("/overview").status_code == 403


def test_manager_lands_on_overview(client):
    response = login(client, "boss")
    assert response.headers["Location"].endswith("/overview")
    assert client.get("/").headers["Location"].endswith("/overview")


def test_overview_in_manager_sidebar_only(client):
    login(client, "boss")
    assert 'href="/overview"' in client.get("/inventory").get_data(as_text=True)
    client.post("/logout")
    login(client, "alice")
    assert 'href="/overview"' not in client.get("/inventory").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Statistics come from the database
# ---------------------------------------------------------------------------

def test_stats_match_database(app, client):
    # Fixture products: Widget qty 10 / reorder 3 (In Stock), Gadget qty 2 / reorder 5 (Low).
    with app.app_context():
        db.session.add_all([
            Product(sku="T-3", name="Empty thing", quantity=0, reorder_level=2),            # Out
            Product(sku="T-4", name="Old thing", quantity=50, reorder_level=1, is_archived=True),  # ignored
        ])
        widget = Product.query.filter_by(sku="T-1").one()
        widget.unit_cost = Decimal("2.50")       # 10 x 2.50 = 25.00
        gadget = Product.query.filter_by(sku="T-2").one()
        gadget.unit_cost = Decimal("10.00")      # 2 x 10.00 = 20.00
        db.session.commit()

    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)
    submit(client, product_id(app, "T-2"), quantity=1)
    client.post("/logout")

    login(client, "boss")
    with app.app_context():
        from manager import overview_stats
        stats = overview_stats(Product.query.filter_by(is_archived=False).all())
    assert stats["total_products"] == 3            # archived product not counted
    assert stats["total_units"] == 12              # 10 + 2 + 0
    assert stats["pending_requests"] == 2
    assert stats["low_stock"] == 1
    assert stats["out_of_stock"] == 1
    assert stats["inventory_value"] == Decimal("45.00")
    assert stats["missing_cost"] == 0              # T-3 has no cost but also no stock

    html = page(client)
    assert "$45.00" in html
    assert "Excludes" not in html


def test_inventory_value_flags_missing_costs(app, client):
    with app.app_context():
        Product.query.filter_by(sku="T-1").one().unit_cost = Decimal("1.00")  # 10 x 1 = 10
        # T-2 (2 in stock) has no unit cost -> excluded and flagged
        db.session.commit()
    login(client, "boss")
    html = page(client)
    assert "$10.00" in html
    assert "Excludes 1 in-stock product with no unit cost" in html


def test_empty_database_shows_zeroes(app, client):
    with app.app_context():
        InventoryRequest.query.delete()
        ActivityLog.query.delete()
        Product.query.delete()
        db.session.commit()
    login(client, "boss")
    html = page(client)
    assert "$0.00" in html
    assert "No pending requests" in html
    assert "Every product is above its reorder level" in html


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def test_pending_section_shows_five_newest(app, client):
    widget = product_id(app, "T-1")
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        start = utc_now()
        for i in range(7):
            db.session.add(InventoryRequest(
                product_id=widget, employee_id=alice.id, request_type="adjustment",
                direction="decrease", quantity=1, reason="Damaged", notes=f"note {i}",
                created_at=start + timedelta(minutes=i),
            ))
        db.session.commit()
        ids = [r.id for r in InventoryRequest.query.order_by(InventoryRequest.created_at).all()]

    login(client, "boss")
    html = page(client)
    for newest in ids[2:]:
        assert f"request #{newest})" in html
    for oldest in ids[:2]:
        assert f"request #{oldest})" not in html
    assert "Showing the 5 newest of 7 pending requests" in html


def test_stock_alerts_list_low_and_out_only(app, client):
    with app.app_context():
        db.session.add(Product(sku="T-3", name="Empty thing", quantity=0, reorder_level=2))
        db.session.commit()
    login(client, "boss")
    html = page(client)
    alerts = html.split("Stock alerts")[1].split("Recent activity")[0]
    assert "Empty thing" in alerts and "Gadget" in alerts
    assert "Widget" not in alerts           # in stock
    assert alerts.index("Empty thing") < alerts.index("Gadget")  # out of stock listed first


def test_recent_activity_shows_inventory_actions_only(app, client):
    login(client, "boss")  # creates a "Logged in" entry with no product
    with app.app_context():
        boss = User.query.filter_by(username="boss").one()
        for i in range(12):
            log_activity(boss.id, f"Inventory action {i}", Product.query.filter_by(sku="T-1").one().id)
        db.session.commit()
    activity = page(client).split("Recent activity")[1]
    assert "Logged in" not in activity
    assert "Inventory action 11" in activity
    assert "Inventory action 1<" not in activity  # only the 10 newest (2..11)


# ---------------------------------------------------------------------------
# Approve / reject from the Overview
# ---------------------------------------------------------------------------

def test_approve_from_overview_changes_stock_and_returns(app, client):
    widget = product_id(app, "T-1")
    login(client, "alice")
    submit(client, widget, quantity=4)
    client.post("/logout")

    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    response = client.post(f"/requests/{request_id}/approve", data={"next": "/overview"})
    assert response.headers["Location"].endswith("/overview")
    with app.app_context():
        assert db.session.get(Product, widget).quantity == 6
        assert db.session.get(InventoryRequest, request_id).status == "Approved"


def test_reject_from_overview_needs_note(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)
    client.post("/logout")

    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/reject", data={"next": "/overview"})
    with app.app_context():
        assert db.session.get(InventoryRequest, request_id).status == "Pending"
    client.post(f"/requests/{request_id}/reject", data={"next": "/overview", "manager_note": "Not damaged"})
    with app.app_context():
        assert db.session.get(InventoryRequest, request_id).status == "Rejected"


def test_decision_redirect_ignores_outside_urls(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    response = client.post(f"/requests/{request_id}/approve", data={"next": "https://evil.example"})
    assert response.headers["Location"].endswith("/approvals")
