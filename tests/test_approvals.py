"""Approvals page: filters, request details, and approval safety rules."""

from conftest import login, product_id
from models import (
    ActivityLog,
    InventoryRequest,
    Product,
    StockMovement,
    User,
    db,
)


def user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def make_request(app, username="alice", sku="T-1", direction="decrease", quantity=1,
                 reason="Damaged", request_type="adjustment", status="Pending", notes="note"):
    """Insert a request straight into the database and return its id."""
    with app.app_context():
        new_request = InventoryRequest(
            product_id=Product.query.filter_by(sku=sku).one().id,
            employee_id=User.query.filter_by(username=username).one().id,
            request_type=request_type,
            direction=None if request_type == "archive" else direction,
            quantity=None if request_type == "archive" else quantity,
            reason="Archive" if request_type == "archive" else reason,
            notes=notes,
            status=status,
        )
        db.session.add(new_request)
        db.session.commit()
        return new_request.id


def html(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def status_of(app, request_id):
    with app.app_context():
        return db.session.get(InventoryRequest, request_id).status


def quantity_of(app, sku):
    with app.app_context():
        return Product.query.filter_by(sku=sku).one().quantity


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def test_employee_cannot_open_approvals(client):
    login(client, "alice")
    assert client.get("/approvals").status_code == 403
    assert client.get("/manager").status_code == 403  # old address


def test_old_address_still_works_for_managers(client):
    login(client, "boss")
    assert client.get("/manager").status_code == 200


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_pending_shown_by_default_and_status_tabs(app, client):
    pending = make_request(app, notes="pending-one")
    make_request(app, notes="approved-one", status="Approved")
    make_request(app, notes="rejected-one", status="Rejected")
    login(client, "boss")

    page = html(client, "/approvals")
    assert "pending-one" in page and "approved-one" not in page and "rejected-one" not in page
    assert f'value="{pending}" form="bulk-review"' in page  # bulk checkbox for pending rows

    page = html(client, "/approvals?status=all")
    assert all(n in page for n in ["pending-one", "approved-one", "rejected-one"])

    page = html(client, "/approvals?status=Rejected")
    assert "rejected-one" in page and "pending-one" not in page
    assert "bulk-review" not in page  # nothing to bulk-review


def test_type_filter(app, client):
    make_request(app, notes="dmg", reason="Damaged")
    make_request(app, notes="miss", reason="Missing")
    make_request(app, notes="up", direction="increase", reason="Received")
    make_request(app, notes="down", reason="Count correction")
    make_request(app, notes="gone", request_type="archive")
    login(client, "boss")

    expected = {"damaged": "dmg", "missing": "miss", "increase": "up", "decrease": "down", "archive": "gone"}
    for type_key, note in expected.items():
        page = html(client, f"/approvals?status=all&type={type_key}")
        assert f"<div>{note}</div>" in page
        for other in set(expected.values()) - {note}:
            assert f"<div>{other}</div>" not in page, (type_key, other)


def test_employee_filter(app, client):
    make_request(app, username="alice", notes="from-alice")
    make_request(app, username="bob", notes="from-bob")
    login(client, "boss")
    page = html(client, f"/approvals?employee={user_id(app, 'bob')}")
    assert "from-bob" in page and "from-alice" not in page


def test_pagination(app, client):
    for i in range(30):
        make_request(app, notes=f"bulk-note-{i}")
    login(client, "boss")
    first = html(client, "/approvals")
    second = html(client, "/approvals?page=2")
    assert "Page 1 of 2" in first and "Page 2 of 2" in second
    assert sum(f"<div>bulk-note-{i}</div>" in first for i in range(30)) == 25
    assert sum(f"<div>bulk-note-{i}</div>" in second for i in range(30)) == 5
    assert "<div>bulk-note-0</div>" in first  # pending queue is oldest first


# ---------------------------------------------------------------------------
# Request details
# ---------------------------------------------------------------------------

def test_request_detail_visibility(app, client):
    request_id = make_request(app, username="alice", notes="private details")

    login(client, "boss")
    assert "private details" in html(client, f"/requests/{request_id}")
    client.post("/logout")

    login(client, "alice")  # the employee who submitted it
    assert "private details" in html(client, f"/requests/{request_id}")
    client.post("/logout")

    login(client, "bob")  # another employee: looks like it doesn't exist
    assert client.get(f"/requests/{request_id}").status_code == 404
    assert client.get("/requests/99999").status_code == 404


def test_employee_detail_page_has_no_decision_buttons(app, client):
    request_id = make_request(app)
    login(client, "alice")
    assert "Approve</button>" not in html(client, f"/requests/{request_id}")


# ---------------------------------------------------------------------------
# Approval safety rules
# ---------------------------------------------------------------------------

def test_approval_updates_stock_movement_and_log_together(app, client):
    request_id = make_request(app, quantity=4)
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve", data={"next": f"/requests/{request_id}"})
    assert status_of(app, request_id) == "Approved"
    assert quantity_of(app, "T-1") == 6
    with app.app_context():
        movement = StockMovement.query.filter_by(request_id=request_id).one()
        assert movement.quantity_change == -4
        assert ActivityLog.query.filter(ActivityLog.action.like(f"Approved request #{request_id}%")).count() == 1
    assert "-4 recorded" in html(client, f"/requests/{request_id}")


def test_duplicate_approval_is_prevented(app, client):
    request_id = make_request(app, quantity=2)
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    response = client.post(f"/requests/{request_id}/approve", follow_redirects=True)
    assert "already processed" in response.get_data(as_text=True)
    assert quantity_of(app, "T-1") == 8  # subtracted once, not twice
    with app.app_context():
        assert StockMovement.query.filter_by(request_id=request_id).count() == 1


def test_insufficient_stock_blocks_approval(app, client):
    request_id = make_request(app, sku="T-2", quantity=5)  # only 2 in stock
    login(client, "boss")
    response = client.post(f"/requests/{request_id}/approve", follow_redirects=True)
    assert "only 2 in stock" in response.get_data(as_text=True)
    assert status_of(app, request_id) == "Pending"
    assert quantity_of(app, "T-2") == 2


def test_cannot_approve_request_for_archived_product(app, client):
    request_id = make_request(app, direction="increase", reason="Found", quantity=1)
    with app.app_context():
        Product.query.filter_by(sku="T-1").one().is_archived = True
        db.session.commit()
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    assert status_of(app, request_id) == "Pending"
    assert quantity_of(app, "T-1") == 10


def test_nobody_reviews_their_own_request(app, client):
    request_id = make_request(app, username="boss")  # (managers can't normally create one)
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/requests/{request_id}/reject", data={"manager_note": "no"})
    assert status_of(app, request_id) == "Pending"
    assert quantity_of(app, "T-1") == 10


def test_employee_cannot_approve_or_reject(app, client):
    request_id = make_request(app, username="alice")
    login(client, "alice")
    assert client.post(f"/requests/{request_id}/approve").status_code == 403
    assert client.post(f"/requests/{request_id}/reject", data={"manager_note": "x"}).status_code == 403
    assert client.post("/requests/bulk-decision", data={"request_ids": [request_id], "decision": "approve"}).status_code == 403
    assert status_of(app, request_id) == "Pending"


def test_rejection_requires_note_and_keeps_stock(app, client):
    request_id = make_request(app, quantity=3)
    login(client, "boss")
    client.post(f"/requests/{request_id}/reject")
    assert status_of(app, request_id) == "Pending"
    client.post(f"/requests/{request_id}/reject", data={"manager_note": "Box was fine"})
    assert status_of(app, request_id) == "Rejected"
    assert quantity_of(app, "T-1") == 10
    with app.app_context():
        assert db.session.get(InventoryRequest, request_id).manager_note == "Box was fine"


def test_bulk_approval_is_all_or_nothing(app, client):
    ok = make_request(app, sku="T-1", quantity=1)
    too_many = make_request(app, sku="T-2", quantity=9)  # only 2 in stock
    login(client, "boss")
    client.post("/requests/bulk-decision", data={"request_ids": [ok, too_many], "decision": "approve"})
    assert status_of(app, ok) == "Pending" and status_of(app, too_many) == "Pending"
    assert quantity_of(app, "T-1") == 10 and quantity_of(app, "T-2") == 2


def test_employee_submission_appears_in_queue(app, client):
    login(client, "alice")
    client.post("/requests", data={
        "product_id": product_id(app, "T-1"), "quantity": 2, "reason": "Missing", "notes": "Shelf empty",
    })
    client.post("/logout")
    login(client, "boss")
    page = html(client, "/approvals")
    assert "Shelf empty" in page and "Missing inventory" in page
