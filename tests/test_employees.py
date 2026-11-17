"""Employee management: access, deactivation rules, and lockout of deactivated users."""

from flask import g

from conftest import PASSWORD, login, product_id, submit
from models import ROLE_MANAGER, ActivityLog, InventoryRequest, User, db


def user(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one()


def add_manager(app, username="boss2"):
    with app.app_context():
        manager = User(username=username, role=ROLE_MANAGER)
        manager.set_password(PASSWORD)
        db.session.add(manager)
        db.session.commit()
        return manager.id


def test_employees_pages_are_manager_only(app, client):
    alice = user(app, "alice")
    login(client, "alice")
    assert client.get("/employees").status_code == 403
    assert client.get(f"/employees/{alice.id}").status_code == 403
    assert client.post(f"/employees/{user(app, 'bob').id}/deactivate").status_code == 403
    assert user(app, "bob").is_active


def test_list_shows_accounts_and_request_counts(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"))
    submit(client, product_id(app, "T-1"))
    client.post("/logout")
    login(client, "boss")
    page = client.get("/employees").get_data(as_text=True)
    for name in ["alice", "bob", "boss"]:
        assert name in page
    alice_row = page.split('<td class="mono">alice</td>')[1].split("</tr>")[0]
    assert '<td class="num">2</td>' in alice_row


def test_profile_shows_requests_and_no_password(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), notes="profile-check")
    client.post("/logout")
    login(client, "boss")
    alice = user(app, "alice")
    page = client.get(f"/employees/{alice.id}").get_data(as_text=True)
    assert "Damaged inventory" in page
    assert alice.password_hash not in page
    assert "pbkdf2" not in page and "scrypt" not in page  # no hash of any kind


def test_deactivated_user_is_locked_out_immediately(app, client):
    alice_client = app.test_client()
    login(alice_client, "alice")
    assert alice_client.get("/employee").status_code == 200

    login(client, "boss")
    alice = user(app, "alice")
    client.post(f"/employees/{alice.id}/deactivate")
    assert not user(app, "alice").is_active

    # Her existing session stops working on the very next request...
    response = alice_client.get("/employee")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    # ...and she can't log in again.
    response = login(app.test_client(), "alice")
    assert response.status_code == 403
    assert "deactivated" in response.get_data(as_text=True)

    with app.app_context():
        entry = ActivityLog.query.filter(ActivityLog.action.like("Deactivated%")).one()
        assert entry.category == "account" and entry.target_user_id == alice.id


def test_wrong_password_for_deactivated_user_gives_generic_message(app, client):
    login(client, "boss")
    client.post(f"/employees/{user(app, 'alice').id}/deactivate")
    response = login(app.test_client(), "alice", "wrong-password")
    assert response.status_code == 401
    assert "deactivated" not in response.get_data(as_text=True)


def test_reactivation(app, client):
    login(client, "boss")
    alice_id = user(app, "alice").id
    client.post(f"/employees/{alice_id}/deactivate")
    client.post(f"/employees/{alice_id}/activate")
    assert user(app, "alice").is_active
    assert login(app.test_client(), "alice").status_code == 302


def test_deactivation_keeps_history(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"))
    client.post("/logout")
    login(client, "boss")
    client.post(f"/employees/{user(app, 'alice').id}/deactivate")
    with app.app_context():
        assert InventoryRequest.query.count() == 1
        assert InventoryRequest.query.one().status == "Pending"


def test_cannot_deactivate_yourself(app, client):
    login(client, "boss")
    response = client.post(f"/employees/{user(app, 'boss').id}/deactivate", follow_redirects=True)
    assert "deactivate your own account" in response.get_data(as_text=True)
    assert user(app, "boss").is_active


def test_manager_can_deactivate_another_manager(app, client):
    boss2 = add_manager(app)
    login(client, "boss")
    client.post(f"/employees/{boss2}/deactivate")
    assert not user(app, "boss2").is_active


def test_last_active_manager_cannot_be_deactivated(app):
    """
    Through the UI the acting manager is always an active manager too, so
    this rule is a safety net. Simulate the edge case directly: boss is
    (somehow) no longer active and boss2 is the only active manager.
    """
    boss2 = add_manager(app)
    with app.app_context():
        User.query.filter_by(username="boss").one().is_active = False
        db.session.commit()

    from employees import deactivate
    with app.test_request_context(f"/employees/{boss2}/deactivate", method="POST"):
        g.user = User.query.filter_by(username="boss").one()
        deactivate(user_id=boss2)
    assert user(app, "boss2").is_active


def test_old_registration_url_creates_nothing(app, client):
    client.post("/register", data={"username": "sneaky", "password": "long-enough-1",
                                   "confirm_password": "long-enough-1", "role": "manager"})
    with app.app_context():
        assert User.query.filter_by(username="sneaky").first() is None
