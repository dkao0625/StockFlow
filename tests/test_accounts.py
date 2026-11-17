"""Manager-created employee accounts (there is no public registration)."""

import re

import pytest

from conftest import PASSWORD, login
from models import ActivityLog, User, db

GOOD = {"full_name": "Dana Lee", "username": "dana", "email": "Dana@Example.com"}


def create(client, **overrides):
    data = dict(GOOD, password_choice="generate")
    data.update(overrides)
    return client.post("/employees/new", data=data)


def get_user(app, username):
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user:
            db.session.expunge(user)
        return user


def shown_password(response):
    match = re.search(r'<p class="temp-password mono">([^<]+)</p>', response.get_data(as_text=True))
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Who can create accounts
# ---------------------------------------------------------------------------

def test_public_cannot_create_accounts(app, client):
    assert client.get("/employees/new").status_code == 302          # login required
    assert create(client).status_code == 302
    assert client.post("/register", data={"username": "x1y", "password": "Abcdefgh12"}).status_code == 404
    assert get_user(app, "dana") is None


def test_employees_cannot_create_accounts(app, client):
    login(client, "alice")
    assert client.get("/employees/new").status_code == 403
    assert create(client).status_code == 403
    assert get_user(app, "dana") is None


# ---------------------------------------------------------------------------
# Creating an employee
# ---------------------------------------------------------------------------

def test_manager_creates_employee_with_generated_password(app, client):
    login(client, "boss")
    response = create(client)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    temporary = shown_password(response)
    assert temporary and len(temporary) == 12

    dana = get_user(app, "dana")
    assert (dana.role, dana.is_active, dana.must_change_password) == ("employee", True, True)
    assert (dana.full_name, dana.email) == ("Dana Lee", "dana@example.com")  # email stored lowercase
    assert dana.created_at is not None
    assert dana.password_hash != temporary and dana.check_password(temporary)  # hashed, and it works

    # Shown only once: not on later pages, and not in the activity log.
    assert temporary not in client.get("/employees").get_data(as_text=True)
    assert temporary not in client.get(f"/employees/{dana.id}").get_data(as_text=True)
    with app.app_context():
        entry = ActivityLog.query.filter_by(category="account", target_user_id=dana.id).one()
        assert entry.action == "Created employee account 'dana'"
        assert temporary not in entry.action


def test_manager_typed_password_is_not_displayed(app, client):
    login(client, "boss")
    response = create(client, password_choice="manual", temporary_password="Warehouse2026")
    assert response.status_code == 200 and shown_password(response) is None
    assert "Warehouse2026" not in response.get_data(as_text=True)
    assert get_user(app, "dana").check_password("Warehouse2026")


def test_form_cannot_create_a_manager(app, client):
    login(client, "boss")
    create(client, role="manager", is_manager="1", must_change_password="0", is_active="0")
    dana = get_user(app, "dana")
    assert (dana.role, dana.must_change_password, dana.is_active) == ("employee", True, True)


def test_generated_passwords_are_strong_and_random():
    from auth import password_problem
    from employees import generate_temporary_password
    passwords = {generate_temporary_password() for _ in range(50)}
    assert len(passwords) == 50
    assert all(password_problem(p) is None for p in passwords)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,message", [
    ({"full_name": "  "}, "Full name is required"),
    ({"full_name": "x" * 101}, "100 characters or fewer"),
    ({"username": "ab"}, "3-30 characters"),
    ({"username": "has space"}, "3-30 characters"),
    ({"username": "alice"}, "already taken"),
    ({"username": "ALICE"}, "already taken"),
    ({"email": ""}, "Email is required"),
    ({"email": "not-an-email"}, "valid email"),
    ({"password_choice": "manual", "temporary_password": "Short1"}, "at least 10 characters"),
    ({"password_choice": "manual", "temporary_password": "onlyletters"}, "letter and one number"),
    ({"password_choice": "manual", "temporary_password": "1234567890"}, "letter and one number"),
    ({"password_choice": "manual", "temporary_password": "dana2026xyz"}, "contain the username"),
])
def test_invalid_input_is_rejected(app, client, overrides, message):
    login(client, "boss")
    response = create(client, **overrides)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True).replace("&#39;", "'")
    with app.app_context():
        assert User.query.count() == 3  # only the fixture users


def test_duplicate_email_is_rejected_case_insensitively(app, client):
    login(client, "boss")
    create(client)
    response = create(client, username="dana2", email="DANA@example.COM")
    assert response.status_code == 400
    assert "already uses that email" in response.get_data(as_text=True)


def test_simultaneous_duplicate_is_caught_by_the_database(app, client, monkeypatch):
    """If two managers submit the same username at once, the UNIQUE constraint wins."""
    import employees
    login(client, "boss")
    create(client)
    # Pretend the friendly uniqueness check didn't see the first account yet.
    real = employees.read_account_form
    monkeypatch.setattr(employees, "read_account_form",
                        lambda form, existing_user=None: (real(form)[0], {}))
    response = create(client, email="other@example.com")
    assert response.status_code == 400
    assert "just taken" in response.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="dana").count() == 1


# ---------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------

def test_new_employee_can_log_in_with_username_or_email(app, client):
    login(client, "boss")
    create(client, password_choice="manual", temporary_password="Warehouse2026")
    client.post("/logout")
    assert login(client, "dana", "Warehouse2026").status_code == 302
    client.post("/logout")
    assert login(client, "DANA@example.com", "Warehouse2026").status_code == 302
    assert get_user(app, "dana").last_login_at is not None


def test_existing_accounts_still_work(app, client):
    response = login(client, "alice", PASSWORD)
    assert response.headers["Location"].endswith("/employee")
    assert get_user(app, "alice").must_change_password is False
    client.post("/logout")
    assert login(client, "boss", PASSWORD).headers["Location"].endswith("/overview")


# ---------------------------------------------------------------------------
# Employees page
# ---------------------------------------------------------------------------

def test_employees_page_lists_new_columns_and_searches(app, client):
    login(client, "boss")
    create(client)
    page = client.get("/employees").get_data(as_text=True)
    assert "+ Add employee" in page and "Dana Lee" in page and "dana@example.com" in page
    assert "Last login" in page and "Temporary password" in page
    for query in ["dana lee", "example.com", "dan"]:
        rows = client.get(f"/employees?q={query}").get_data(as_text=True).split("<tbody>")[1]
        assert "Dana Lee" in rows and ">alice<" not in rows
