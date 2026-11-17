"""Employee dashboard and My Requests: personal stats, recent items, privacy."""

from datetime import timedelta

from conftest import login, product_id
from models import ActivityLog, InventoryRequest, Product, User, db, utc_now

# Fixture products: Widget T-1 qty 10 / reorder 3 (In Stock), Gadget T-2 qty 2 / reorder 5 (Low).


def add_request(app, username, status="Pending", notes="note", minutes_ago=0, manager_note=None):
    with app.app_context():
        new_request = InventoryRequest(
            product_id=Product.query.filter_by(sku="T-1").one().id,
            employee_id=User.query.filter_by(username=username).one().id,
            request_type="adjustment", direction="decrease", quantity=1, reason="Damaged",
            notes=notes, status=status, manager_note=manager_note,
            created_at=utc_now() - timedelta(minutes=minutes_ago),
        )
        db.session.add(new_request)
        db.session.commit()
        return new_request.id


def dashboard(client):
    response = client.get("/employee")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def section(page, start, end):
    return page.split(start)[1].split(end)[0]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def test_stats_count_only_my_requests(app, client):
    for status in ["Pending", "Pending", "Approved", "Received", "Rejected", "Needs Correction"]:
        add_request(app, "alice", status=status)
    add_request(app, "bob", status="Pending")  # someone else's: not counted
    login(client, "alice")
    from inventory import employee_stats
    with app.test_request_context():
        from flask import g
        g.user = User.query.filter_by(username="alice").one()
        stats = employee_stats()
    assert stats == {"pending": 2, "needs_correction": 1, "approved": 2, "rejected": 1, "alerts": 1}


def test_cards_show_numbers_and_link_to_filtered_pages(app, client):
    add_request(app, "alice", status="Pending")
    login(client, "alice")
    page = dashboard(client)
    assert 'href="/my-requests?status=pending"' in page
    assert 'href="/my-requests?status=approved"' in page
    assert 'href="/my-requests?status=rejected"' in page  # nothing to correct -> rejected list
    assert 'href="/alerts"' in page
    pending_card = section(page, "My pending requests", "</a>")
    assert '<div class="stat-value">1</div>' in pending_card


def test_low_stock_alert_count_matches_alerts_page(app, client):
    with app.app_context():
        db.session.add(Product(sku="T-3", name="Empty", quantity=0, reorder_level=1))
        db.session.add(Product(sku="T-4", name="Gone", quantity=0, reorder_level=1, is_archived=True))
        db.session.commit()
    login(client, "alice")
    card = section(dashboard(client), "Low stock alerts", "</a>")
    assert '<div class="stat-value">2</div>' in card  # Gadget (low) + Empty (out); archived ignored


def test_dashboard_works_with_no_products(app, client):
    with app.app_context():
        Product.query.delete()
        db.session.commit()
    login(client, "alice")
    page = dashboard(client)
    assert "There are no active products yet" in page


# ---------------------------------------------------------------------------
# Recent requests and activity
# ---------------------------------------------------------------------------

def test_recent_requests_shows_my_five_newest(app, client):
    for i in range(7):
        add_request(app, "alice", notes=f"n{i}", minutes_ago=10 - i,
                    manager_note=f"feedback-{i}" if i == 6 else None)
    add_request(app, "bob", manager_note="bob-feedback")
    login(client, "alice")
    recent = section(dashboard(client), "Recent requests", "Recent activity")
    assert recent.count("View details") == 5
    assert "feedback-6" in recent          # manager feedback shown
    assert "bob-feedback" not in recent    # other employees' requests are private
    assert 'href="/my-requests"' in recent


def test_recent_activity_is_mine_and_inventory_only(app, client):
    login(client, "alice")  # writes a "Logged in" entry (not inventory work)
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        bob = User.query.filter_by(username="bob").one()
        widget = Product.query.filter_by(sku="T-1").one().id
        for i in range(6):
            db.session.add(ActivityLog(actor_id=alice.id, action=f"alice-action-{i}", category="product_edited",
                                       product_id=widget, created_at=utc_now() + timedelta(seconds=i)))
        db.session.add(ActivityLog(actor_id=bob.id, action="bob-action", category="product_edited", product_id=widget))
        db.session.commit()
    activity = dashboard(client).split("Recent activity")[1]
    assert "alice-action-5" in activity and "alice-action-1" in activity
    assert "alice-action-0" not in activity  # only the 5 newest
    assert "bob-action" not in activity
    assert "Logged in" not in activity


# ---------------------------------------------------------------------------
# My Requests page
# ---------------------------------------------------------------------------

def test_my_requests_filters_by_status(app, client):
    add_request(app, "alice", status="Pending", notes="p-note")
    add_request(app, "alice", status="Approved", notes="a-note")
    add_request(app, "alice", status="Partially Received", notes="pr-note")
    add_request(app, "alice", status="Rejected", notes="r-note", manager_note="Not damaged")
    login(client, "alice")

    with app.app_context():
        ids = {r.notes: r.id for r in InventoryRequest.query.all()}

    def ids_shown(status):
        page = client.get(f"/my-requests?status={status}").get_data(as_text=True)
        return {note for note, rid in ids.items() if f"/requests/{rid}\"" in page}

    assert ids_shown("all") == {"p-note", "a-note", "pr-note", "r-note"}
    assert ids_shown("pending") == {"p-note"}
    assert ids_shown("approved") == {"a-note", "pr-note"}
    assert ids_shown("rejected") == {"r-note"}
    assert "Not damaged" in client.get("/my-requests?status=rejected").get_data(as_text=True)


def test_my_requests_only_shows_own(app, client):
    bob_request = add_request(app, "bob", notes="bob-only")
    login(client, "alice")
    page = client.get("/my-requests").get_data(as_text=True)
    assert f'/requests/{bob_request}"' not in page
    assert client.get(f"/requests/{bob_request}").status_code == 404


def test_my_requests_pagination(app, client):
    for i in range(25):
        add_request(app, "alice", minutes_ago=i)
    login(client, "alice")
    first = client.get("/my-requests").get_data(as_text=True)
    second = client.get("/my-requests?page=2").get_data(as_text=True)
    assert first.count("View details") == 20 and second.count("View details") == 5
    assert "Page 1 of 2" in first


def test_my_requests_access(app, client):
    assert client.get("/my-requests").status_code == 302  # login required
    login(client, "boss")
    assert client.get("/my-requests").status_code == 403  # managers use Approvals


# ---------------------------------------------------------------------------
# Navigation and shared inventory
# ---------------------------------------------------------------------------

def test_employee_sidebar(client):
    login(client, "alice")
    page = client.get("/inventory").get_data(as_text=True)
    nav = page.split('<nav class="nav">')[1].split("</nav>")[0]
    for link in ["/employee", "/my-requests", "/inventory", "/alerts", "/my-activity"]:
        assert f'href="{link}"' in nav
    for manager_link in ["/overview", "/approvals", "/employees", "/reports", '"/activity"']:
        assert manager_link not in nav


def test_employees_and_managers_see_the_same_inventory(app, client):
    login(client, "alice")
    employee_page = client.get("/inventory").get_data(as_text=True)
    client.post("/logout")
    login(client, "boss")
    manager_page = client.get("/inventory").get_data(as_text=True)
    for page in (employee_page, manager_page):
        assert "Widget" in page and "Gadget" in page
        stats = section(page, '<section class="stats">', "</section>")
        assert '<div class="stat-value">2</div>' in stats   # products
        assert '<div class="stat-value">12</div>' in stats  # units on hand
