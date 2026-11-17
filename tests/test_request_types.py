"""Report a problem: every request type, its validation, and what approval does."""

import pytest

from conftest import login, product_id
from models import InventoryRequest, Product, StockMovement, db
from stock import archive_blocker

# Fixture products: Widget T-1 qty 10 / reorder 3, Gadget T-2 qty 2 / reorder 5.


def report(client, app, request_type, sku="T-1", **fields):
    data = {"product_id": product_id(app, sku), "request_type": request_type, "notes": "Explanation"}
    data.update(fields)
    return client.post("/requests", data=data)


def only_request(app):
    with app.app_context():
        r = InventoryRequest.query.one()
        db.session.expunge(r)
        return r


def request_count(app):
    with app.app_context():
        return InventoryRequest.query.count()


def quantity_of(app, sku="T-1"):
    with app.app_context():
        return Product.query.filter_by(sku=sku).one().quantity


def movements(app):
    with app.app_context():
        return [m.quantity_change for m in StockMovement.query.order_by(StockMovement.id)]


def approve_as_boss(client, app):
    client.post("/logout")
    login(client, "boss")
    request_id = only_request(app).id
    response = client.post(f"/requests/{request_id}/approve", follow_redirects=True)
    return request_id, response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Damaged / missing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("request_type,reason", [("damaged", "Damaged"), ("missing", "Missing")])
def test_damaged_and_missing_reports(app, client, request_type, reason):
    login(client, "alice")
    report(client, app, request_type, quantity="3")
    r = only_request(app)
    assert (r.request_type, r.reason, r.direction, r.quantity, r.status) == ("adjustment", reason, "decrease", 3, "Pending")
    assert quantity_of(app) == 10  # nothing changes until approval
    approve_as_boss(client, app)
    assert quantity_of(app) == 7


def test_damaged_needs_positive_quantity_within_stock(app, client):
    login(client, "alice")
    for bad in ["0", "-1", "1.5", "", "11"]:  # only 10 in stock
        report(client, app, "damaged", quantity=bad)
    assert request_count(app) == 0


# ---------------------------------------------------------------------------
# Stock count correction
# ---------------------------------------------------------------------------

def test_count_correction_sets_stock_to_counted_value(app, client):
    login(client, "alice")
    report(client, app, "count", counted_quantity="7")
    r = only_request(app)
    assert (r.request_type, r.counted_quantity, r.quantity, r.status) == ("count", 7, None, "Pending")
    assert quantity_of(app) == 10
    approve_as_boss(client, app)
    assert quantity_of(app) == 7
    assert movements(app) == [-3]


def test_count_difference_uses_stock_at_approval_time(app, client):
    login(client, "alice")
    report(client, app, "count", counted_quantity="12",
           difference="-100", recorded_quantity="1")  # browser-sent numbers are ignored
    client.post("/logout")
    login(client, "boss")
    client.post(f"/products/{product_id(app, 'T-1')}/restock", data={"quantity": "5"})  # now 15
    request_id = only_request(app).id
    client.post(f"/requests/{request_id}/approve")
    assert quantity_of(app) == 12
    assert movements(app) == [5, -3]  # the restock, then 15 -> 12


def test_count_of_zero_is_valid(app, client):
    login(client, "alice")
    report(client, app, "count", sku="T-2", counted_quantity="0")
    approve_as_boss(client, app)
    assert quantity_of(app, "T-2") == 0
    assert movements(app) == [-2]


def test_count_matching_the_record_changes_nothing(app, client):
    login(client, "alice")
    report(client, app, "count", counted_quantity="10")  # matches the record
    approve_as_boss(client, app)
    assert only_request(app).status == "Approved"
    assert quantity_of(app) == 10 and movements(app) == []


def test_count_validation(app, client):
    login(client, "alice")
    for bad in ["-1", "abc", "", "2.5", "1000001"]:
        report(client, app, "count", counted_quantity=bad)
    assert request_count(app) == 0


def test_count_correction_can_be_corrected_and_resubmitted(app, client):
    login(client, "alice")
    report(client, app, "count", counted_quantity="4")
    client.post("/logout")
    login(client, "boss")
    request_id = only_request(app).id
    client.post(f"/requests/{request_id}/request-correction", data={"manager_note": "Recount shelf B too"})
    client.post("/logout")
    login(client, "alice")
    client.post(f"/requests/{request_id}/resubmit", data={"quantity": "6", "response": "Shelf B had 2 more"})
    with app.app_context():
        r = InventoryRequest.query.one()
        assert (r.status, r.counted_quantity, r.original_quantity) == ("Pending", 6, 4)
    approve_as_boss(client, app)
    assert quantity_of(app) == 6


# ---------------------------------------------------------------------------
# Stock adjustment (increase / decrease)
# ---------------------------------------------------------------------------

def test_adjustment_increase(app, client):
    login(client, "alice")
    report(client, app, "adjust", direction="increase", adjust_reason="Found", quantity="4")
    r = only_request(app)
    assert (r.direction, r.reason, r.quantity, r.type_label) == ("increase", "Found", 4, "Stock increase")
    assert quantity_of(app) == 10
    approve_as_boss(client, app)
    assert quantity_of(app) == 14


def test_adjustment_decrease_checks_stock(app, client):
    login(client, "alice")
    report(client, app, "adjust", sku="T-2", direction="decrease", adjust_reason="Count correction", quantity="3")
    assert request_count(app) == 0  # only 2 in stock
    report(client, app, "adjust", sku="T-2", direction="decrease", adjust_reason="Count correction", quantity="2")
    approve_as_boss(client, app)
    assert quantity_of(app, "T-2") == 0


def test_adjustment_validation(app, client):
    login(client, "alice")
    report(client, app, "adjust", direction="sideways", adjust_reason="Found", quantity="1")
    report(client, app, "adjust", direction="increase", adjust_reason="Damaged", quantity="1")  # wrong reason
    report(client, app, "adjust", direction="decrease", adjust_reason="Found", quantity="1")    # wrong reason
    report(client, app, "adjust", direction="increase", adjust_reason="Found", quantity="0")
    assert request_count(app) == 0


# ---------------------------------------------------------------------------
# Restock and other issues
# ---------------------------------------------------------------------------

def test_restock_from_report_form_uses_restock_workflow(app, client):
    login(client, "alice")
    report(client, app, "restock", sku="T-2", quantity="20", additional_notes="Supplier: Acme")
    r = only_request(app)
    assert (r.request_type, r.quantity, r.status, r.additional_notes) == ("restock", 20, "Pending", "Supplier: Acme")
    request_id, _page = approve_as_boss(client, app)
    assert quantity_of(app, "T-2") == 2  # approval doesn't add stock
    client.post(f"/restock/{request_id}/receive", data={"quantity": "20"})
    assert quantity_of(app, "T-2") == 22


def test_other_issue_never_changes_stock(app, client):
    login(client, "alice")
    report(client, app, "issue", quantity="999", counted_quantity="0", direction="decrease")  # ignored
    r = only_request(app)
    assert (r.request_type, r.reason, r.quantity, r.counted_quantity) == ("issue", "Other", None, None)
    _id, page = approve_as_boss(client, app)
    assert "marked resolved" in page
    assert only_request(app).status == "Approved"
    assert quantity_of(app) == 10 and movements(app) == []


def test_explanation_required_for_every_type(app, client):
    login(client, "alice")
    for request_type, fields in [
        ("damaged", {"quantity": "1"}), ("count", {"counted_quantity": "5"}),
        ("adjust", {"direction": "increase", "adjust_reason": "Found", "quantity": "1"}),
        ("restock", {"quantity": "5"}), ("issue", {}),
    ]:
        report(client, app, request_type, notes="  ", **fields)
    assert request_count(app) == 0


def test_additional_notes_are_optional_but_limited(app, client):
    login(client, "alice")
    report(client, app, "issue", additional_notes="x" * 501)
    assert request_count(app) == 0
    report(client, app, "issue")
    assert only_request(app).additional_notes is None


def test_unknown_types_are_rejected(app, client):
    login(client, "alice")
    for bad in ["archive", "approve", "", "restock'--"]:
        report(client, app, bad, quantity="1")
    assert request_count(app) == 0


def test_form_error_returns_to_form_with_type_selected(app, client):
    login(client, "alice")
    response = report(client, app, "count", counted_quantity="-5")
    assert "type=count" in response.headers["Location"]
    assert response.headers["Location"].endswith("#report")


def test_managers_cannot_use_the_employee_form(app, client):
    login(client, "boss")
    assert report(client, app, "issue").status_code == 403


# ---------------------------------------------------------------------------
# Effects elsewhere
# ---------------------------------------------------------------------------

def test_pending_count_blocks_archiving(app, client):
    login(client, "alice")
    report(client, app, "count", counted_quantity="1")
    with app.app_context():
        assert archive_blocker(Product.query.filter_by(sku="T-1").one()) is not None


def test_new_types_in_approvals_filter(app, client):
    login(client, "alice")
    report(client, app, "count", counted_quantity="8", notes="count-note")
    report(client, app, "issue", notes="issue-note")
    client.post("/logout")
    login(client, "boss")
    counts = client.get("/approvals?type=count").get_data(as_text=True)
    issues = client.get("/approvals?type=issue").get_data(as_text=True)
    assert "count-note" in counts and "issue-note" not in counts
    assert "issue-note" in issues and "count-note" not in issues
    assert "counted <strong>8</strong>" in counts
