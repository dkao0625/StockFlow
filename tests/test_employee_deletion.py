"""Soft-deleting employee accounts and the Deleted employees history."""

from datetime import datetime, timezone

import pytest

from conftest import PASSWORD, login, product_id, submit
from models import ActivityLog, InventoryRequest, User, db

REASON = "Employee left the company"


def user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def get_user(app, username):
    with app.app_context():
        user = User.query.filter_by(username=username).one()
        db.session.expunge(user)
        return user


def delete(client, uid, reason=REASON):
    return client.post(f"/employees/{uid}/delete", data={"reason": reason})


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------

def test_manager_soft_deletes_an_employee(app, client):
    alice_id = user_id(app, "alice")
    login(client, "boss")
    response = delete(client, alice_id)
    assert response.headers["Location"].endswith("/employees?status=deleted")

    alice = get_user(app, "alice")
    assert (alice.is_deleted, alice.is_active, alice.deletion_reason) == (True, False, REASON)
    assert alice.deleted_by_id == user_id(app, "boss") and alice.deleted_at is not None
    with app.app_context():
        assert db.session.get(User, alice_id) is not None  # the row still exists
        entry = ActivityLog.query.filter_by(category="employee_deleted").one()
        assert entry.target_user_id == alice_id and REASON in entry.action
        assert alice.password_hash not in entry.action


def test_reason_is_required(app, client):
    login(client, "boss")
    for reason in ["", "   ", "x" * 501]:
        response = delete(client, user_id(app, "alice"), reason)
        assert response.status_code == 400
    assert get_user(app, "alice").is_deleted is False


def test_confirmation_shows_details_and_warning(app, client):
    login(client, "boss")
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        alice.full_name, alice.email = "Alice Wong", "alice@example.com"
        db.session.commit()
        alice_id = alice.id
    for url in [f"/employees/{alice_id}/delete", f"/employees/{alice_id}"]:  # page and dialog
        page = client.get(url).get_data(as_text=True)
        assert "Alice Wong" in page and "alice@example.com" in page
        assert "The employee will no longer be able to log in." in page
        assert "Delete Employee</button>" in page and 'name="reason"' in page


def test_employees_cannot_delete_anyone(app, client):
    login(client, "alice")
    for target in ["bob", "alice", "boss"]:
        assert delete(client, user_id(app, target)).status_code == 403
        assert client.get(f"/employees/{user_id(app, target)}/delete").status_code == 403
    assert not any(get_user(app, u).is_deleted for u in ["alice", "bob", "boss"])


def test_managers_cannot_delete_managers_or_themselves(app, client):
    with app.app_context():
        other = User(username="boss2", role="manager")
        other.set_password(PASSWORD)
        db.session.add(other)
        db.session.commit()
    login(client, "boss")
    assert delete(client, user_id(app, "boss2")).status_code == 403
    assert delete(client, user_id(app, "boss")).status_code == 403
    assert client.post("/employees/99999/delete", data={"reason": REASON}).status_code == 404
    assert not get_user(app, "boss2").is_deleted and not get_user(app, "boss").is_deleted


def test_unauthenticated_delete_is_rejected(app, client):
    response = delete(client, user_id(app, "alice"))
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert get_user(app, "alice").is_deleted is False


# ---------------------------------------------------------------------------
# Access after deletion
# ---------------------------------------------------------------------------

def test_deleted_employee_loses_session_and_cannot_log_in(app, client):
    alice_device = app.test_client()
    login(alice_device, "alice")
    assert alice_device.get("/employee").status_code == 200

    login(client, "boss")
    delete(client, user_id(app, "alice"))

    response = alice_device.get("/employee")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    response = login(app.test_client(), "alice")
    assert response.status_code == 403 and "has been deleted" in response.get_data(as_text=True)
    wrong = login(app.test_client(), "alice", "wrong-password")
    assert wrong.status_code == 401 and "deleted" not in wrong.get_data(as_text=True)  # no hint without the password


def test_deleted_account_cannot_be_reactivated_edited_or_reset(app, client):
    alice_id = user_id(app, "alice")
    login(client, "boss")
    delete(client, alice_id)
    client.post(f"/employees/{alice_id}/activate")
    assert get_user(app, "alice").is_active is False
    assert client.get(f"/employees/{alice_id}/edit").status_code == 409
    assert client.post(f"/employees/{alice_id}/reset-password").status_code == 409
    response = delete(client, alice_id, "again")  # already deleted
    assert response.status_code == 302
    with app.app_context():
        assert ActivityLog.query.filter_by(category="employee_deleted").count() == 1


# ---------------------------------------------------------------------------
# Lists and history
# ---------------------------------------------------------------------------

def table(page):
    return page.split("<tbody>")[1].split("</tbody>")[0]


def test_deleted_employee_leaves_normal_lists_and_appears_in_deleted_view(app, client):
    login(client, "boss")
    delete(client, user_id(app, "alice"))
    for url in ["/employees", "/employees?status=active", "/employees?status=deactivated",
                "/employees?role=employee"]:
        assert '<td class="mono">alice</td>' not in table(client.get(url).get_data(as_text=True)), url
    deleted = client.get("/employees?status=deleted").get_data(as_text=True)
    rows = table(deleted)
    assert '<td class="mono">alice</td>' in rows and REASON in rows and ">boss<" in rows
    assert "Deleted (1)" in deleted and '<td class="mono">bob</td>' not in rows


def test_deleted_view_search_and_dates(app, client):
    login(client, "boss")
    delete(client, user_id(app, "alice"))
    delete(client, user_id(app, "bob"), "Contract ended")
    with app.app_context():
        User.query.filter_by(username="bob").one().deleted_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
        db.session.commit()

    def shown(query):
        rows = table(client.get(f"/employees?status=deleted&{query}").get_data(as_text=True))
        return {u for u in ["alice", "bob"] if f'<td class="mono">{u}</td>' in rows}

    assert shown("q=bob") == {"bob"}
    assert shown("deleted_to=2026-01-31") == {"bob"}
    assert shown("deleted_from=2026-02-01") == {"alice"}
    assert shown("deleted_from=bad-date") == {"alice", "bob"}


def test_history_is_preserved_with_original_identity(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=1, notes="alice's report")
    client.post("/logout")
    with app.app_context():
        request_id = InventoryRequest.query.one().id
    login(client, "boss")
    alice_id = user_id(app, "alice")
    delete(client, alice_id)

    with app.app_context():
        assert InventoryRequest.query.one().employee_id == alice_id
    approvals = client.get("/approvals").get_data(as_text=True)
    assert "alice" in table(approvals)                               # still shown by name
    assert client.get(f"/requests/{request_id}").status_code == 200
    assert "alice" in client.get(f"/activity?user={alice_id}").get_data(as_text=True)
    profile = client.get(f"/employees/{alice_id}").get_data(as_text=True)
    assert "This account was deleted" in profile and REASON in profile
    assert "Deletion history" in profile
    assert "Requests submitted" in profile

    client.post(f"/requests/{request_id}/approve")                     # approvals still work
    with app.app_context():
        assert InventoryRequest.query.one().status == "Approved"


def test_activity_page_shows_deletions(app, client):
    login(client, "boss")
    delete(client, user_id(app, "alice"))
    page = client.get("/activity?category=employee_deleted").get_data(as_text=True)
    rows = table(page)
    assert "Employee deleted" in rows and "Deleted employee account &#39;alice&#39;" in rows and REASON in rows


@pytest.mark.parametrize("url", ["/employees?status=deleted"])
def test_deleted_view_is_manager_only(app, client, url):
    login(client, "alice")
    assert client.get(url).status_code == 403


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------

import re  # noqa: E402


def restore(client, uid, note=""):
    return client.post(f"/employees/{uid}/restore", data={"note": note})


def shown_password(response):
    return re.search(r'<p class="temp-password mono">([^<]+)</p>', response.get_data(as_text=True)).group(1)


def test_manager_restores_a_deleted_employee(app, client):
    alice_id = user_id(app, "alice")
    login(client, "boss")
    delete(client, alice_id)
    response = restore(client, alice_id, note="Rehired")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert "Employee restored" in response.get_data(as_text=True)
    temporary = shown_password(response)

    alice = get_user(app, "alice")
    assert (alice.is_deleted, alice.is_active, alice.must_change_password) == (False, True, True)
    assert alice.check_password(temporary) and not alice.check_password(PASSWORD)
    # The last deletion record is kept on the account...
    assert alice.deletion_reason == REASON and alice.deleted_at is not None
    with app.app_context():
        # ...and the log has BOTH the deletion and the restoration.
        categories = [e.category for e in ActivityLog.query.filter_by(target_user_id=alice_id)
                      .filter(ActivityLog.category.in_(["employee_deleted", "employee_restored"]))
                      .order_by(ActivityLog.id)]
        assert categories == ["employee_deleted", "employee_restored"]
        restored = ActivityLog.query.filter_by(category="employee_restored").one()
        assert restored.actor_id == user_id(app, "boss") and "Note: Rehired" in restored.action
        assert temporary not in restored.action

    profile = client.get(f"/employees/{alice_id}").get_data(as_text=True)
    assert "Active" in profile and "This account was deleted" not in profile
    assert "Deletion history" in profile and "Employee deleted" in profile and "Employee restored" in profile
    assert temporary not in profile  # shown only once


def test_restored_employee_must_change_password_and_old_session_stays_dead(app, client):
    alice_device = app.test_client()
    login(alice_device, "alice")
    alice_id = user_id(app, "alice")
    login(client, "boss")
    delete(client, alice_id)
    temporary = shown_password(restore(client, alice_id))

    assert alice_device.get("/employee").status_code == 302          # old session doesn't come back
    assert login(app.test_client(), "alice", PASSWORD).status_code == 401  # old password gone
    fresh = app.test_client()
    assert login(fresh, "alice", temporary).headers["Location"].endswith("/change-password")
    assert fresh.get("/employee").headers["Location"].endswith("/change-password")


def test_restore_rules(app, client):
    alice_id, boss_id = user_id(app, "alice"), user_id(app, "boss")
    login(client, "boss")
    response = restore(client, alice_id)                     # not deleted
    assert response.status_code == 302 and get_user(app, "alice").must_change_password is False
    assert restore(client, boss_id).status_code == 403       # managers aren't restored here
    assert restore(client, 99999).status_code == 404
    delete(client, alice_id)
    client.post("/logout")
    login(client, "bob")
    assert restore(client, alice_id).status_code == 403      # employees can't restore
    assert get_user(app, "alice").is_deleted is True


def test_deleted_list_has_restore_with_confirmation(app, client):
    alice_id = user_id(app, "alice")
    login(client, "boss")
    delete(client, alice_id)
    page = client.get("/employees?status=deleted").get_data(as_text=True)
    assert f'action="/employees/{alice_id}/restore"' in page and 'data-confirm="Restore alice?' in page


def test_deleted_accounts_keep_their_username_and_email(app, client):
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        alice.email = "alice@example.com"
        db.session.commit()
    login(client, "boss")
    delete(client, user_id(app, "alice"))
    for fields, field_text in [({"username": "alice", "email": "new@example.com"}, "username belongs to a deleted account"),
                               ({"username": "alice2", "email": "ALICE@example.com"}, "email belongs to a deleted account")]:
        response = client.post("/employees/new", data=dict(
            {"full_name": "New Person", "password_choice": "generate"}, **fields))
        assert response.status_code == 400 and field_text in response.get_data(as_text=True)


def test_delete_restore_delete_keeps_every_entry(app, client):
    alice_id = user_id(app, "alice")
    login(client, "boss")
    delete(client, alice_id, "First reason")
    restore(client, alice_id)
    delete(client, alice_id, "Second reason")
    alice = get_user(app, "alice")
    assert alice.deletion_reason == "Second reason"
    with app.app_context():
        actions = [e.action for e in ActivityLog.query.filter_by(target_user_id=alice_id)
                   .filter(ActivityLog.category.in_(["employee_deleted", "employee_restored"]))]
    assert len(actions) == 3 and any("First reason" in a for a in actions) and any("Second reason" in a for a in actions)
