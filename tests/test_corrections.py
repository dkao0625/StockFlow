"""Request correction and resubmission: manager <-> employee workflow."""

from conftest import login, product_id, submit
from models import (
    ActivityLog,
    InventoryRequest,
    Product,
    RequestEvent,
    StockMovement,
    User,
    db,
)
from stock import archive_blocker

# Fixture products: Widget T-1 qty 10 / reorder 3, Gadget T-2 qty 2 / reorder 5.


def get_request(app, request_id):
    with app.app_context():
        r = db.session.get(InventoryRequest, request_id)
        db.session.expunge(r)
        return r


def quantity_of(app, sku):
    with app.app_context():
        return Product.query.filter_by(sku=sku).one().quantity


def events_of(app, request_id):
    with app.app_context():
        return [(e.event, e.quantity_before, e.quantity_after, e.note)
                for e in RequestEvent.query.filter_by(request_id=request_id).order_by(RequestEvent.id)]


def alice_reports(app, client, quantity=5, sku="T-1"):
    """alice reports damaged items; returns the request id. Leaves nobody logged in."""
    login(client, "alice")
    submit(client, product_id(app, sku), quantity=quantity, notes="Boxes crushed")
    client.post("/logout")
    with app.app_context():
        return InventoryRequest.query.order_by(InventoryRequest.id.desc()).first().id


def send_back(client, request_id, note="Please verify the quantity."):
    login(client, "boss")
    response = client.post(f"/requests/{request_id}/request-correction", data={"manager_note": note})
    client.post("/logout")
    return response


# ---------------------------------------------------------------------------
# The full example from start to finish
# ---------------------------------------------------------------------------

def test_full_correction_workflow(app, client):
    # 1. Employee reports five damaged boxes.
    request_id = alice_reports(app, client, quantity=5)

    # 2-4. Manager asks for a correction.
    send_back(client, request_id)
    r = get_request(app, request_id)
    assert r.status == "Needs Correction"
    assert r.manager_note == "Please verify the quantity."
    assert quantity_of(app, "T-1") == 10  # nothing changed

    # 5. Employee sees the feedback (dashboard banner + detail page).
    login(client, "alice")
    assert "need" in client.get("/employee").get_data(as_text=True) and "your correction" in client.get("/employee").get_data(as_text=True)
    detail = client.get(f"/requests/{request_id}").get_data(as_text=True)
    assert "Correction requested" in detail and "Please verify the quantity." in detail
    assert "Resubmit for review" in detail

    # 6-9. Employee changes 5 -> 3, explains, resubmits: back to Pending.
    client.post(f"/requests/{request_id}/resubmit",
                data={"quantity": "3", "response": "Recounted, only 3 are damaged."})
    r = get_request(app, request_id)
    assert (r.status, r.quantity, r.manager_note) == ("Pending", 3, None)
    client.post("/logout")

    # 10-11. Manager approves the corrected request: stock drops by 3.
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    assert get_request(app, request_id).status == "Approved"
    assert quantity_of(app, "T-1") == 7
    with app.app_context():
        assert StockMovement.query.filter_by(request_id=request_id).one().quantity_change == -3

    # History keeps the original submission, the feedback, the resubmission, and the approval.
    assert events_of(app, request_id) == [
        ("correction_requested", None, None, "Please verify the quantity."),
        ("resubmitted", 5, 3, "Recounted, only 3 are damaged."),
        ("approved", None, None, None),
    ]
    with app.app_context():
        assert db.session.get(InventoryRequest, request_id).original_quantity == 5
        categories = {e.category for e in ActivityLog.query.all()}
        assert {"correction_requested", "request_resubmitted"} <= categories
    page = client.get(f"/requests/{request_id}").get_data(as_text=True)
    assert "Originally submitted" in page and "Quantity changed 5 → 3" in page


# ---------------------------------------------------------------------------
# Manager: Request correction
# ---------------------------------------------------------------------------

def test_request_correction_needs_a_note(app, client):
    request_id = alice_reports(app, client)
    send_back(client, request_id, note="   ")
    assert get_request(app, request_id).status == "Pending"


def test_only_pending_requests_can_be_sent_back(app, client):
    request_id = alice_reports(app, client)
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/requests/{request_id}/request-correction", data={"manager_note": "too late"})
    assert get_request(app, request_id).status == "Approved"


def test_employees_cannot_request_corrections(app, client):
    request_id = alice_reports(app, client)
    login(client, "bob")
    response = client.post(f"/requests/{request_id}/request-correction", data={"manager_note": "x"})
    assert response.status_code == 403
    assert get_request(app, request_id).status == "Pending"


def test_needs_correction_cannot_be_approved_until_resubmitted(app, client):
    request_id = alice_reports(app, client, quantity=2)
    send_back(client, request_id)
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    client.post("/requests/bulk-decision", data={"request_ids": [request_id], "decision": "approve"})
    assert get_request(app, request_id).status == "Needs Correction"
    assert quantity_of(app, "T-1") == 10


# ---------------------------------------------------------------------------
# Employee: Resubmit
# ---------------------------------------------------------------------------

def test_only_the_owner_can_resubmit(app, client):
    request_id = alice_reports(app, client)
    send_back(client, request_id)
    login(client, "bob")
    response = client.post(f"/requests/{request_id}/resubmit", data={"quantity": "1", "response": "mine now"})
    assert response.status_code == 404
    client.post("/logout")
    login(client, "boss")
    assert client.post(f"/requests/{request_id}/resubmit", data={"quantity": "1", "response": "x"}).status_code == 403
    r = get_request(app, request_id)
    assert (r.status, r.quantity) == ("Needs Correction", 5)


def test_only_needs_correction_requests_can_be_resubmitted(app, client):
    pending = alice_reports(app, client, quantity=2)
    rejected = alice_reports(app, client, quantity=2)
    login(client, "boss")
    client.post(f"/requests/{rejected}/reject", data={"manager_note": "No"})
    client.post("/logout")

    login(client, "alice")
    for request_id in (pending, rejected):
        client.post(f"/requests/{request_id}/resubmit", data={"quantity": "1", "response": "edit"})
    assert (get_request(app, pending).status, get_request(app, pending).quantity) == ("Pending", 2)
    assert (get_request(app, rejected).status, get_request(app, rejected).quantity) == ("Rejected", 2)
    assert events_of(app, pending) == []


def test_resubmission_is_validated(app, client):
    request_id = alice_reports(app, client, quantity=5)
    send_back(client, request_id)
    login(client, "alice")
    for bad in [
        {"quantity": "0", "response": "fixed"},
        {"quantity": "-3", "response": "fixed"},
        {"quantity": "abc", "response": "fixed"},
        {"quantity": "11", "response": "fixed"},   # only 10 in stock
        {"quantity": "3", "response": "  "},       # response required
    ]:
        client.post(f"/requests/{request_id}/resubmit", data=bad)
    r = get_request(app, request_id)
    assert (r.status, r.quantity) == ("Needs Correction", 5)


def test_duplicate_approval_after_resubmission_is_prevented(app, client):
    request_id = alice_reports(app, client, quantity=4)
    send_back(client, request_id)
    login(client, "alice")
    client.post(f"/requests/{request_id}/resubmit", data={"quantity": "4", "response": "Count is right"})
    client.post("/logout")
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/requests/{request_id}/approve")
    assert quantity_of(app, "T-1") == 6
    with app.app_context():
        assert StockMovement.query.filter_by(request_id=request_id).count() == 1


def test_restock_correction_does_not_change_stock(app, client):
    login(client, "alice")
    client.post("/restock/new", data={"product_id": product_id(app, "T-2"), "quantity": 50, "notes": "Low"})
    client.post("/logout")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    send_back(client, request_id, note="50 is too many, max 20")
    login(client, "alice")
    client.post(f"/requests/{request_id}/resubmit", data={"quantity": "20", "response": "Reduced to 20"})
    client.post("/logout")
    login(client, "boss")
    client.post(f"/requests/{request_id}/approve")
    r = get_request(app, request_id)
    assert (r.status, r.quantity) == ("Approved", 20)
    assert quantity_of(app, "T-2") == 2  # approval authorizes; receiving adds stock


def test_archive_request_resubmits_without_quantity(app, client):
    login(client, "alice")
    widget = product_id(app, "T-1")
    client.post("/bulk/archive", data={"product_ids": [widget], "note": "Discontinued"})
    client.post("/logout")
    with app.app_context():
        request_id = InventoryRequest.query.filter_by(request_type="archive").one().id
    send_back(client, request_id, note="Which supplier discontinued it?")
    login(client, "alice")
    client.post(f"/requests/{request_id}/resubmit", data={"response": "Acme, per their email"})
    assert get_request(app, request_id).status == "Pending"


# ---------------------------------------------------------------------------
# Effects elsewhere
# ---------------------------------------------------------------------------

def test_request_awaiting_correction_blocks_archiving(app, client):
    request_id = alice_reports(app, client, quantity=1)
    send_back(client, request_id)
    with app.app_context():
        assert "pending stock request" in archive_blocker(Product.query.filter_by(sku="T-1").one())


def test_my_requests_needs_correction_tab_type_filter_and_search(app, client):
    corrected = alice_reports(app, client, quantity=1)
    other = alice_reports(app, client, quantity=1, sku="T-2")
    send_back(client, corrected)
    login(client, "alice")

    page = client.get("/my-requests?status=needs_correction").get_data(as_text=True)
    assert f'/requests/{corrected}"' in page and f'/requests/{other}"' not in page
    assert "Fix &amp; resubmit" in page

    by_type = client.get("/my-requests?type=restock").get_data(as_text=True)
    assert f'/requests/{corrected}"' not in by_type

    by_product = client.get("/my-requests?q=gadget").get_data(as_text=True)
    assert f'/requests/{other}"' in by_product and f'/requests/{corrected}"' not in by_product
    by_id = client.get(f"/my-requests?q=%23{corrected}").get_data(as_text=True)
    assert f'/requests/{corrected}"' in by_id and f'/requests/{other}"' not in by_id


def test_manager_approvals_has_needs_correction_tab_and_button(app, client):
    request_id = alice_reports(app, client)
    login(client, "boss")
    assert "Request correction" in client.get("/approvals").get_data(as_text=True)
    client.post(f"/requests/{request_id}/request-correction", data={"manager_note": "Check it"})
    page = client.get("/approvals?status=Needs Correction").get_data(as_text=True)
    assert "Boxes crushed" in page


def test_manager_cannot_send_back_own_request(app, client):
    with app.app_context():
        boss = User.query.filter_by(username="boss").one()
        own = InventoryRequest(product_id=Product.query.filter_by(sku="T-1").one().id, employee_id=boss.id,
                               request_type="adjustment", direction="decrease", quantity=1,
                               reason="Damaged", notes="x", status="Pending")
        db.session.add(own)
        db.session.commit()
        request_id = own.id
    login(client, "boss")
    client.post(f"/requests/{request_id}/request-correction", data={"manager_note": "hmm"})
    assert get_request(app, request_id).status == "Pending"
