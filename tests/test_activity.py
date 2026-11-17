"""Activity log: what gets recorded, filters, CSV export, and read-only protection."""

import csv
import io
from datetime import datetime, timezone

import pytest

from conftest import PASSWORD, login, product_id, submit
from models import ActivityLog, InventoryRequest, Product, User, db


def entries(app, category):
    with app.app_context():
        return ActivityLog.query.filter_by(category=category).order_by(ActivityLog.id).all()


def user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def add_entry(app, username, action, created_at, category="product_edited"):
    with app.app_context():
        db.session.add(ActivityLog(
            actor_id=User.query.filter_by(username=username).one().id,
            actor_role="employee", category=category, action=action, created_at=created_at,
        ))
        db.session.commit()


def read_csv(response):
    text = response.get_data(as_text=True).lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# What is recorded
# ---------------------------------------------------------------------------

def test_inventory_actions_are_categorized(app, client):
    login(client, "alice")
    widget = product_id(app, "T-1")
    client.post("/products/new", data={
        "name": "Stapler", "sku": "ST-1", "category": "Office Supplies", "unit": "Pieces",
        "quantity": "4", "reorder_level": "1",
    })
    client.post(f"/products/{widget}/edit", data={
        "name": "Widget Pro", "sku": "T-1", "category": "Other", "unit": "Pieces",
    })
    submit(client, widget, quantity=2, reason="Damaged")
    client.post("/logout")

    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/approve")
    client.post(f"/products/{widget}/restock", data={"quantity": "5", "note": "Delivery"})

    created = entries(app, "product_created")[0]
    assert created.actor_role == "employee" and created.quantity_change == 4
    edit = entries(app, "product_edited")[0]
    assert "Widget" in edit.action and "Widget Pro" in edit.action  # before and after values
    assert entries(app, "damage_report")[0].product_id == widget
    approved = entries(app, "request_approved")[0]
    assert approved.actor_role == "manager"
    decrease = entries(app, "stock_decrease")[0]
    assert decrease.quantity_change == -2
    increase = entries(app, "stock_increase")[0]
    assert increase.quantity_change == 5
    assert len(entries(app, "login")) >= 3  # logins and logout


def test_rejection_and_bulk_edit_are_recorded(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"))
    client.post("/logout")
    login(client, "boss")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    client.post(f"/requests/{request_id}/reject", data={"manager_note": "Not damaged"})
    client.post("/bulk/edit", data={
        "product_ids": [product_id(app, "T-1"), product_id(app, "T-2")],
        "update_location": "1", "location": "Aisle 9",
    })
    assert len(entries(app, "request_rejected")) == 1
    assert len(entries(app, "bulk_edit")) == 2


def test_passwords_are_never_logged(app, client):
    login(client, "boss")
    client.post("/employees/new", data={
        "full_name": "New Bie", "username": "newbie", "email": "newbie@example.com",
        "password_choice": "manual", "temporary_password": "secret-pass-123",
    })
    client.post("/logout")
    login(client, "newbie", "secret-pass-123")
    login(client, "alice")
    with app.app_context():
        for entry in ActivityLog.query.all():
            assert "secret-pass-123" not in entry.action
            assert PASSWORD not in entry.action
        creation = ActivityLog.query.filter_by(category="account").one()
        assert creation.target_user.username == "newbie"


def test_activity_rows_cannot_be_changed_or_deleted(app, client):
    login(client, "alice")
    with app.app_context():
        entry = ActivityLog.query.first()
        entry.action = "tampered"
        with pytest.raises(PermissionError):
            db.session.commit()
        db.session.rollback()
        with pytest.raises(PermissionError):
            db.session.delete(ActivityLog.query.first())
            db.session.commit()
        db.session.rollback()
        assert ActivityLog.query.filter_by(action="tampered").count() == 0


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def test_employee_cannot_open_full_log_or_export(client):
    login(client, "alice")
    assert client.get("/activity").status_code == 403
    assert client.get("/activity/export.csv").status_code == 403


def test_my_activity_shows_only_own_entries(app, client):
    add_entry(app, "bob", "bob-secret-action", datetime.now(timezone.utc))
    login(client, "alice")
    add_entry(app, "alice", "alice-own-action", datetime.now(timezone.utc))
    page = client.get("/my-activity?user=" + str(user_id(app, "bob"))).get_data(as_text=True)
    assert "alice-own-action" in page
    assert "bob-secret-action" not in page


# ---------------------------------------------------------------------------
# Filters and pages
# ---------------------------------------------------------------------------

def test_filters(app, client):
    add_entry(app, "alice", "alice-old", datetime(2026, 1, 5, 12, tzinfo=timezone.utc))
    add_entry(app, "alice", "alice-new", datetime(2026, 3, 9, 23, 30, tzinfo=timezone.utc), category="damage_report")
    add_entry(app, "bob", "bob-new", datetime(2026, 3, 9, 8, tzinfo=timezone.utc))
    login(client, "boss")

    def shown(query):
        page = client.get("/activity?" + query).get_data(as_text=True)
        return {name for name in ["alice-old", "alice-new", "bob-new"] if name in page}

    assert shown(f"user={user_id(app, 'bob')}") == {"bob-new"}
    assert shown("category=damage_report") == {"alice-new"}
    assert shown("q=ALICE-") == {"alice-old", "alice-new"}
    assert shown("date_from=2026-03-01") == {"alice-new", "bob-new"}
    assert shown("date_to=2026-01-31") == {"alice-old"}
    assert shown("date_from=2026-03-09&date_to=2026-03-09") == {"alice-new", "bob-new"}  # whole day
    assert shown("date_from=not-a-date") == {"alice-old", "alice-new", "bob-new"}
    assert shown("role=manager") == set()


def test_pagination(app, client):
    for i in range(60):
        add_entry(app, "alice", f"entry-{i:02d}-end", datetime(2026, 2, 1, tzinfo=timezone.utc))
    login(client, "boss")
    first = client.get("/activity?q=entry-").get_data(as_text=True)
    second = client.get("/activity?q=entry-&page=2").get_data(as_text=True)
    assert "Page 1 of 2" in first
    assert sum(f"entry-{i:02d}-end" in first for i in range(60)) == 50
    assert sum(f"entry-{i:02d}-end" in second for i in range(60)) == 10
    assert "q=entry-" in first  # next-page link keeps the filter


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_csv_export_respects_filters_and_is_safe(app, client):
    add_entry(app, "alice", "=HYPERLINK(\"http://evil\")", datetime(2026, 3, 1, tzinfo=timezone.utc))
    add_entry(app, "bob", "bob-row", datetime(2026, 3, 1, tzinfo=timezone.utc))
    login(client, "boss")
    response = client.get(f"/activity/export.csv?user={user_id(app, 'alice')}")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="stockflow-activity-') and disposition.endswith('.csv"')

    rows = read_csv(response)
    assert rows[0][:4] == ["Timestamp (UTC)", "User", "Role", "Action"]
    body = "\n".join(",".join(r) for r in rows)
    assert "bob-row" not in body
    descriptions = [r[-1] for r in rows[1:]]
    assert "'=HYPERLINK(\"http://evil\")" in descriptions  # neutralized formula
    assert "pbkdf2" not in body and "scrypt" not in body     # no password hashes
