"""Authentication and permission tests."""

from conftest import PASSWORD, login, product_id, submit
from models import InventoryRequest, User, db


def test_register_always_creates_employee(app, client):
    # Even if someone sneaks role=manager into the form, they get "employee".
    response = client.post(
        "/register",
        data={
            "username": "Newbie",
            "password": "longenough1",
            "confirm_password": "longenough1",
            "role": "manager",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        user = User.query.filter_by(username="newbie").one()
        assert user.role == "employee"
        assert user.password_hash != "longenough1"  # stored hashed


def test_register_rejects_short_password_and_duplicates(client):
    short = client.post(
        "/register", data={"username": "carl", "password": "short", "confirm_password": "short"}
    )
    assert short.status_code == 400
    duplicate = client.post(
        "/register",
        data={"username": "alice", "password": "longenough1", "confirm_password": "longenough1"},
    )
    assert duplicate.status_code == 400


def test_login_success_and_failure(client):
    assert login(client, "alice", "wrong-password").status_code == 401
    response = login(client, "alice")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/employee")


def test_pages_require_login(client):
    for url in ["/inventory", "/employee", "/manager"]:
        response = client.get(url)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_employee_cannot_use_manager_routes(app, client):
    login(client, "bob")
    submit(client, product_id(app, "T-1"))
    client.post("/logout")
    login(client, "alice")

    assert client.get("/manager").status_code == 403
    with app.app_context():
        request_id = InventoryRequest.query.first().id
    assert client.post(f"/requests/{request_id}/approve").status_code == 403
    assert client.post(f"/requests/{request_id}/reject", data={"manager_note": "x"}).status_code == 403
    with app.app_context():
        assert db.session.get(InventoryRequest, request_id).status == "Pending"


def test_manager_cannot_submit_employee_reports(app, client):
    login(client, "boss")
    assert submit(client, product_id(app, "T-1")).status_code == 403


def test_employee_sees_only_own_requests(app, client):
    login(client, "bob")
    submit(client, product_id(app, "T-1"), notes="BOB-SECRET-NOTE")
    client.post("/logout")

    login(client, "alice")
    page = client.get("/employee").get_data(as_text=True)
    assert "BOB-SECRET-NOTE" not in page


def test_logout_invalidates_old_session_cookie(app, client):
    login(client, "alice")
    # Copy the session cookie as an attacker might.
    stolen_cookie = client.get_cookie("session").value

    client.post("/logout")

    # Replay the stolen cookie in a fresh client: it must not work anymore.
    attacker = app.test_client()
    attacker.set_cookie("session", stolen_cookie)
    response = attacker.get("/employee")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_csrf_token_required_when_enabled(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post("/login", data={"username": "alice", "password": PASSWORD})
    assert response.status_code == 400
