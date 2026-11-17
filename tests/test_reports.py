"""Reports page and CSV exports: real numbers, date ranges, permissions, safe output."""

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import login, product_id, submit
from models import InventoryRequest, Product, StockMovement, User, db

# Fixture products: Widget T-1 qty 10 / reorder 3, Gadget T-2 qty 2 / reorder 5.

EXPORT_URLS = [
    "/inventory/export.csv",
    "/alerts/export.csv",
    "/approvals/export.csv",
    "/activity/export.csv",
    "/reports/movements.csv",
    "/reports/requests.csv",
]


def read_csv(response):
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    return list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("﻿"))))


def add_movement(app, sku, change, when, note="Count", request_id=None):
    with app.app_context():
        db.session.add(StockMovement(
            product_id=Product.query.filter_by(sku=sku).one().id, quantity_change=change,
            created_at=when, note=note, request_id=request_id,
            actor_id=User.query.filter_by(username="boss").one().id,
        ))
        db.session.commit()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/reports"] + EXPORT_URLS)
def test_employees_cannot_open_reports_or_exports(client, url):
    login(client, "alice")
    assert client.get(url).status_code == 403


@pytest.mark.parametrize("url", ["/reports"] + EXPORT_URLS)
def test_reports_and_exports_require_login(client, url):
    assert client.get(url).status_code == 302


@pytest.mark.parametrize("url", EXPORT_URLS)
def test_every_export_downloads_for_managers(client, url):
    login(client, "boss")
    response = client.get(url)
    rows = read_csv(response)
    assert len(rows) >= 1  # at least the header row
    assert response.headers["Content-Disposition"].startswith('attachment; filename="stockflow-')


# ---------------------------------------------------------------------------
# Reports page
# ---------------------------------------------------------------------------

def test_empty_reports_page(app, client):
    login(client, "boss")
    page = client.get("/reports").get_data(as_text=True)
    assert "No stock moved in this date range" in page
    assert "No requests were submitted in this date range" in page


def test_movement_report_totals_and_date_range(app, client):
    now = datetime.now(timezone.utc)
    add_movement(app, "T-1", 20, now - timedelta(days=1), note="Delivery")
    add_movement(app, "T-1", -3, now - timedelta(days=1), note="Damaged: dropped")  # manager adjustment
    add_movement(app, "T-2", -1, now, note="Count correction")
    add_movement(app, "T-2", 50, now - timedelta(days=90), note="Too old")           # outside default range

    login(client, "boss")
    from reports import movement_report, range_bounds
    with app.test_request_context():
        start, end = range_bounds((now - timedelta(days=29)).date(), now.date())
        totals, days, biggest = movement_report(start, end)
    assert (totals["added"], totals["removed"], totals["damage"], totals["net"]) == (20, 4, 3, 16)
    assert totals["count"] == 3 and biggest == 20 and len(days) == 2

    page = client.get("/reports").get_data(as_text=True)
    assert "+20" in page and "−4" in page

    old_day = (now - timedelta(days=90)).date().isoformat()
    page = client.get(f"/reports?date_from={old_day}&date_to={old_day}").get_data(as_text=True)
    assert "+50" in page and "+20" not in page


def test_damage_from_approved_requests_is_counted(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=2, reason="Missing")
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/approve")
    from reports import movement_report, range_bounds
    with app.test_request_context():
        today = datetime.now(timezone.utc).date()
        totals, _days, _biggest = movement_report(*range_bounds(today, today))
    assert totals["damage"] == 2 and totals["removed"] == 2


def test_request_report_by_status_and_employee(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1)
    submit(client, product_id(app, "T-1"), quantity=1)
    client.post("/logout")
    login(client, "bob")
    submit(client, product_id(app, "T-1"), quantity=1)
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        first, second, third = [r.id for r in InventoryRequest.query.order_by(InventoryRequest.id)]
    client.post(f"/requests/{first}/approve")
    client.post(f"/requests/{third}/reject", data={"manager_note": "No"})

    from reports import range_bounds, request_report
    with app.test_request_context():
        today = datetime.now(timezone.utc).date()
        totals, employees, types = request_report(*range_bounds(today, today))
    assert totals == {"total": 3, "pending": 1, "approved": 1, "rejected": 1}
    assert employees[0] == {"username": "alice", "total": 2, "pending": 1, "approved": 1, "rejected": 0}
    assert employees[1]["username"] == "bob" and employees[1]["rejected"] == 1
    assert types == {"Damaged inventory": 3}


def test_inventory_summary_uses_known_costs(app, client):
    with app.app_context():
        Product.query.filter_by(sku="T-1").one().unit_cost = Decimal("3.00")
        db.session.commit()
    login(client, "boss")
    page = client.get("/reports").get_data(as_text=True)
    assert "$30.00" in page
    assert "Excludes 1 in-stock product with no unit cost" in page


def test_reversed_dates_are_swapped(app, client):
    login(client, "boss")
    response = client.get("/reports?date_from=2026-03-10&date_to=2026-03-01", follow_redirects=True)
    page = response.get_data(as_text=True)
    assert 'value="2026-03-01"' in page and 'value="2026-03-10"' in page


# ---------------------------------------------------------------------------
# Exports respect filters and are safe
# ---------------------------------------------------------------------------

def test_inventory_export_respects_filters(app, client):
    login(client, "boss")
    rows = read_csv(client.get("/inventory/export.csv?status=Low Stock"))
    assert rows[0][:3] == ["SKU", "Name", "Category"]
    assert [r[0] for r in rows[1:]] == ["T-2"]
    rows = read_csv(client.get("/inventory/export.csv?q=widget"))
    assert [r[0] for r in rows[1:]] == ["T-1"]


def test_alerts_export_respects_filters(app, client):
    with app.app_context():
        db.session.add(Product(sku="T-3", name="Empty", quantity=0, reorder_level=1))
        db.session.commit()
    login(client, "boss")
    assert [r[0] for r in read_csv(client.get("/alerts/export.csv"))[1:]] == ["T-3", "T-2"]
    assert [r[0] for r in read_csv(client.get("/alerts/export.csv?status=out"))[1:]] == ["T-3"]


def test_requests_export_respects_status_filter(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1, notes="first-one")
    submit(client, product_id(app, "T-1"), quantity=1, notes="second-one")
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        first = InventoryRequest.query.order_by(InventoryRequest.id).first().id
    client.post(f"/requests/{first}/approve")
    pending = read_csv(client.get("/approvals/export.csv"))  # default: Pending
    column = pending[0].index("Explanation")
    assert [r[column] for r in pending[1:]] == ["second-one"]
    everything = read_csv(client.get("/approvals/export.csv?status=all"))
    assert len(everything) == 3


def test_exports_neutralize_formulas(app, client):
    with app.app_context():
        widget = Product.query.filter_by(sku="T-1").one()
        widget.name = "=cmd|' /C calc'!A0"
        widget.supplier = "@SUM(A1:A9)"
        widget.location = "+1 Aisle"
        db.session.commit()
    login(client, "boss")
    rows = read_csv(client.get("/inventory/export.csv?q=T-1"))
    header, row = rows[0], rows[1]
    assert row[header.index("Name")] == "'=cmd|' /C calc'!A0"
    assert row[header.index("Supplier")] == "'@SUM(A1:A9)"
    assert row[header.index("Location")] == "'+1 Aisle"
    assert row[header.index("Quantity")] == "10"  # real numbers are left alone


def test_exports_never_include_secrets(app, client):
    login(client, "boss")
    with app.app_context():
        hashes = [u.password_hash for u in User.query.all()]
    for url in EXPORT_URLS:
        body = client.get(url + "?status=all").get_data(as_text=True)
        assert not any(h in body for h in hashes), url
        assert app.config["SECRET_KEY"] not in body
        assert "session" not in body.lower()


def test_safe_cell_and_filename():
    from csv_export import safe_cell, safe_filename
    assert safe_cell("-5 boxes") == "'-5 boxes"
    assert safe_cell("\t=1+1") == "'\t=1+1"
    assert safe_cell(-5) == -5
    assert safe_cell(None) == ""
    assert safe_filename('../../etc/"passwd"\r\n') == "etc-passwd"
