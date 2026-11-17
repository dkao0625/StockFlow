"""First-login password change, manager password reset, and editing employees."""

import re

import pytest

from conftest import PASSWORD, login, product_id
from models import ActivityLog, InventoryRequest, User, db

TEMP = "Temporary2026"
NEW = "MyOwnPass99"


def make_employee(client, username="dana", password=TEMP):
    """boss creates an employee with a typed temporary password, then logs out."""
    login(client, "boss")
    client.post("/employees/new", data={
        "full_name": "Dana Lee", "username": username, "email": f"{username}@example.com",
        "password_choice": "manual", "temporary_password": password,
    })
    client.post("/logout")


def get_user(app, username):
    with app.app_context():
        user = User.query.filter_by(username=username).one()
        db.session.expunge(user)
        return user


def change(client, current=TEMP, new=NEW, confirm=None):
    return client.post("/change-password", data={
        "current_password": current, "new_password": new, "confirm_password": confirm or new,
    })


# ---------------------------------------------------------------------------
# First login
# ---------------------------------------------------------------------------

def test_first_login_goes_to_change_password(app, client):
    make_employee(client)
    response = login(client, "dana", TEMP)
    assert response.headers["Location"].endswith("/change-password")
    page = client.get("/change-password").get_data(as_text=True)
    assert "Choose your password" in page
    assert 'href="/inventory"' not in page  # menu hidden until the password is changed


@pytest.mark.parametrize("method,url", [
    ("get", "/employee"), ("get", "/inventory"), ("get", "/my-requests"), ("get", "/alerts"),
    ("get", "/my-activity"), ("get", "/restock/new"), ("get", "/products/1"),
    ("post", "/requests"), ("post", "/restock/new"), ("post", "/bulk/edit"),
])
def test_temporary_password_blocks_everything_else(app, client, method, url):
    make_employee(client)
    login(client, "dana", TEMP)
    response = getattr(client, method)(url, data={"product_id": product_id(app, "T-1"), "quantity": "1",
                                                  "reason": "Damaged", "notes": "x"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/change-password")
    with app.app_context():
        assert InventoryRequest.query.count() == 0


def test_logout_still_works_with_temporary_password(app, client):
    make_employee(client)
    login(client, "dana", TEMP)
    response = client.post("/logout")
    assert response.headers["Location"].endswith("/login")


def test_changing_the_password_unlocks_the_app(app, client):
    make_employee(client)
    login(client, "dana", TEMP)
    response = change(client)
    assert response.headers["Location"].endswith("/employee")
    assert client.get("/employee").status_code == 200

    dana = get_user(app, "dana")
    assert dana.must_change_password is False
    assert dana.check_password(NEW) and not dana.check_password(TEMP)
    assert dana.password_hash != NEW
    with app.app_context():
        entry = ActivityLog.query.filter_by(action="Changed temporary password (first login)").one()
        assert entry.target_user_id == dana.id and NEW not in entry.action

    client.post("/logout")
    assert login(client, "dana", TEMP).status_code == 401           # old password is gone
    assert login(client, "dana", NEW).headers["Location"].endswith("/employee")


@pytest.mark.parametrize("current,new,confirm,message", [
    ("WrongPass123", NEW, NEW, "current password is incorrect"),
    (TEMP, NEW, "Different123", "don&#39;t match"),
    (TEMP, TEMP, TEMP, "different from your current"),
    (TEMP, "short1", "short1", "at least 10 characters"),
    (TEMP, "nodigitshere", "nodigitshere", "letter and one number"),
    (TEMP, "dana123456", "dana123456", "contain the username"),
])
def test_new_password_is_validated(app, client, current, new, confirm, message):
    make_employee(client)
    login(client, "dana", TEMP)
    response = change(client, current, new, confirm)
    assert response.status_code == 400 and message in response.get_data(as_text=True)
    assert get_user(app, "dana").must_change_password is True


def test_changing_password_logs_out_other_sessions(app, client):
    other_device = app.test_client()
    login(other_device, "alice")
    login(client, "alice")
    change(client, current=PASSWORD, new="BrandNew2026")
    assert client.get("/employee").status_code == 200           # this session keeps working
    assert other_device.get("/employee").status_code == 302     # the other one is logged out


# ---------------------------------------------------------------------------
# Manager password reset
# ---------------------------------------------------------------------------

def test_manager_resets_employee_password(app, client):
    alice_device = app.test_client()
    login(alice_device, "alice")
    login(client, "boss")
    alice = get_user(app, "alice")
    response = client.post(f"/employees/{alice.id}/reset-password")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    temporary = re.search(r'<p class="temp-password mono">([^<]+)</p>', response.get_data(as_text=True)).group(1)

    alice = get_user(app, "alice")
    assert alice.must_change_password is True and alice.check_password(temporary)
    assert not alice.check_password(PASSWORD)
    assert alice_device.get("/employee").status_code == 302      # her open session ended
    assert temporary not in client.get(f"/employees/{alice.id}").get_data(as_text=True)  # shown once
    with app.app_context():
        entry = ActivityLog.query.filter(ActivityLog.action.like("Reset the password%")).one()
        assert entry.target_user_id == alice.id and temporary not in entry.action

    # Next login: forced to choose a new password.
    client.post("/logout")
    assert login(client, "alice", temporary).headers["Location"].endswith("/change-password")
    change(client, current=temporary, new="Shelving2026x")
    with app.app_context():
        assert ActivityLog.query.filter_by(action="Changed temporary password (first login)").count() == 1


def test_reset_password_permissions(app, client):
    alice_id, boss_id = get_user(app, "alice").id, get_user(app, "boss").id
    login(client, "bob")
    assert client.post(f"/employees/{alice_id}/reset-password").status_code == 403
    client.post("/logout")
    login(client, "boss")
    assert client.post(f"/employees/{boss_id}/reset-password").status_code == 403  # not managers
    assert client.get(f"/employees/{alice_id}/reset-password").status_code == 405  # POST only
    assert get_user(app, "alice").check_password(PASSWORD)


# ---------------------------------------------------------------------------
# Edit employee
# ---------------------------------------------------------------------------

def edit(client, user_id, **fields):
    data = {"full_name": "Alice Smith", "username": "alice", "email": "alice@example.com", "is_active": "1"}
    data.update(fields)
    return client.post(f"/employees/{user_id}/edit", data=data)


def test_manager_edits_employee_and_history_is_kept(app, client):
    login(client, "alice")
    client.post("/requests", data={"product_id": product_id(app, "T-1"), "quantity": "1",
                                   "reason": "Damaged", "notes": "x"})
    client.post("/logout")
    alice_id = get_user(app, "alice").id
    login(client, "boss")
    edit(client, alice_id, full_name="Alice Smith", username="asmith", email="Alice@Example.com", role="manager")

    alice = get_user(app, "asmith")
    assert (alice.full_name, alice.email, alice.role) == ("Alice Smith", "alice@example.com", "employee")
    with app.app_context():
        assert InventoryRequest.query.one().employee_id == alice_id  # history still linked
        entry = ActivityLog.query.filter(ActivityLog.action.like("Updated employee%")).one()
        assert "username 'alice' → 'asmith'" in entry.action
        assert "alice@example.com" not in entry.action  # email values aren't logged
    client.post("/logout")
    assert login(client, "asmith", PASSWORD).status_code == 302


def test_edit_validates_uniqueness(app, client):
    alice_id = get_user(app, "alice").id
    login(client, "boss")
    edit(client, get_user(app, "bob").id, full_name="Bob", username="bob", email="taken@example.com")
    for fields, message in [({"username": "bob"}, "already taken"),
                            ({"email": "TAKEN@example.com"}, "already uses that email"),
                            ({"full_name": ""}, "Full name is required")]:
        response = edit(client, alice_id, **fields)
        assert response.status_code == 400 and message in response.get_data(as_text=True)
    assert get_user(app, "alice").username == "alice"


def test_deactivating_through_edit_logs_them_out(app, client):
    alice_device = app.test_client()
    login(alice_device, "alice")
    login(client, "boss")
    edit(client, get_user(app, "alice").id, is_active="0")
    assert get_user(app, "alice").is_active is False
    assert alice_device.get("/employee").status_code == 302


def test_edit_permissions(app, client):
    alice_id, boss_id = get_user(app, "alice").id, get_user(app, "boss").id
    login(client, "bob")
    assert client.get(f"/employees/{alice_id}/edit").status_code == 403
    assert edit(client, alice_id, full_name="Hacked").status_code == 403
    client.post("/logout")
    login(client, "boss")
    assert client.get(f"/employees/{boss_id}/edit").status_code == 403  # manager accounts aren't edited here
    assert get_user(app, "alice").full_name is None


def test_employee_list_has_account_actions(app, client):
    login(client, "boss")
    page = client.get("/employees").get_data(as_text=True)
    alice_id = get_user(app, "alice").id
    for action in [f"/employees/{alice_id}/edit", f"/employees/{alice_id}/reset-password",
                   f"/employees/{alice_id}/deactivate"]:
        assert action in page
    assert "data-confirm=\"Reset the password" in page and "data-confirm=\"Deactivate" in page
