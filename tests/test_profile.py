"""My Profile page and the clickable sidebar account link."""

import pytest

from conftest import login, product_id, submit
from models import ActivityLog, User, db


def set_details(app, username, **fields):
    with app.app_context():
        user = User.query.filter_by(username=username).one()
        for name, value in fields.items():
            setattr(user, name, value)
        db.session.commit()
        return user.id


def profile_page(client):
    response = client.get("/profile")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_profile_requires_login(client):
    response = client.get("/profile")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


@pytest.mark.parametrize("username,role", [("alice", "Employee"), ("boss", "Manager")])
def test_both_roles_see_their_own_profile(app, client, username, role):
    set_details(app, username, full_name="Pat Morgan", email=f"{username}@example.com")
    login(client, username)
    page = profile_page(client)
    assert "Pat Morgan" in page and f"{username}@example.com" in page
    assert f'<span class="mono">{username}</span>' in page
    assert f">{role}</span>" in page and "Active" in page
    assert ">PM</span>" in page                       # initials avatar
    assert "Last login" in page and "Account created" in page


def test_profile_ignores_ids_in_the_url(app, client):
    bob_id = set_details(app, "bob", full_name="Bob Secret", email="bob@example.com")
    login(client, "alice")
    for url in [f"/profile?user_id={bob_id}", f"/profile?id={bob_id}&username=bob"]:
        page = client.get(url).get_data(as_text=True)
        assert "Bob Secret" not in page and "bob@example.com" not in page
        assert '<span class="mono">alice</span>' in page
    assert client.get(f"/profile/{bob_id}").status_code == 404


def test_missing_details_show_placeholders(app, client):
    login(client, "alice")  # fixture users have no full name or email
    page = profile_page(client)
    assert "<h1>alice</h1>" in page and "Not set" in page
    assert ">AL</span>" in page


def test_recent_activity_is_mine_only_and_limited(app, client):
    login(client, "alice")
    for i in range(6):
        submit(client, product_id(app, "T-1"), quantity=1, notes=f"note {i}")
    with app.app_context():
        bob = User.query.filter_by(username="bob").one()
        db.session.add(ActivityLog(actor_id=bob.id, action="bob-private-action", category="product_edited"))
        db.session.commit()
    activity = profile_page(client).split("Recent activity")[1]
    assert activity.count("<tr>") - 1 == 5               # header row + 5 entries
    assert "bob-private-action" not in activity
    assert 'href="/my-activity"' in activity


def test_empty_activity_message(app, client):
    with app.app_context():
        ActivityLog.query.delete()
        db.session.commit()
    login(client, "alice")
    with app.app_context():
        ActivityLog.query.delete()  # remove the login entry too
        db.session.commit()
    assert "Nothing yet" in profile_page(client)


@pytest.mark.parametrize("username", ["alice", "boss"])
def test_sidebar_account_area_links_to_profile(app, client, username):
    login(client, username)
    page = client.get("/inventory").get_data(as_text=True)
    sidebar_user = page.split('<div class="sidebar-user">')[1].split("</aside>")[0]
    assert 'class="sidebar-profile' in sidebar_user and 'href="/profile"' in sidebar_user
    assert f'<span class="user-name">{username}</span>' in sidebar_user
    assert 'action="/logout"' in sidebar_user        # log out is still its own button
    # The profile link is highlighted while on the profile page.
    assert 'class="sidebar-profile active"' in client.get("/profile").get_data(as_text=True)


def test_logout_still_works(client):
    login(client, "alice")
    assert client.post("/logout").headers["Location"].endswith("/login")
    assert client.get("/profile").status_code == 302


def test_temporary_password_users_must_change_it_first(app, client):
    set_details(app, "alice", must_change_password=True)
    login(client, "alice")
    assert client.get("/profile").headers["Location"].endswith("/change-password")


# ---------------------------------------------------------------------------
# Edit profile
# ---------------------------------------------------------------------------

def edit(client, **fields):
    data = {"full_name": "Pat Morgan", "email": "pat@example.com"}
    data.update(fields)
    return client.post("/profile/edit", data=data)


def get_user(app, username):
    with app.app_context():
        user = User.query.filter_by(username=username).one()
        db.session.expunge(user)
        return user


@pytest.mark.parametrize("username", ["alice", "boss"])
def test_users_edit_their_own_name_and_email(app, client, username):
    login(client, username)
    form = client.get("/profile/edit").get_data(as_text=True)
    assert "Save changes" in form and "Cancel" in form
    response = edit(client, email="Pat@Example.COM")
    assert response.headers["Location"].endswith("/profile")
    user = get_user(app, username)
    assert (user.full_name, user.email) == ("Pat Morgan", "pat@example.com")
    page = client.get("/profile").get_data(as_text=True)
    assert "Your profile was updated." in page and "Pat Morgan" in page
    with app.app_context():
        entry = ActivityLog.query.filter(ActivityLog.action.like("Updated own profile%")).one()
        assert entry.target_user_id == user.id
        assert "pat@example.com" not in entry.action and "Pat Morgan" not in entry.action


def test_cannot_change_role_status_username_or_someone_else(app, client):
    bob_id = get_user(app, "bob").id
    login(client, "alice")
    edit(client, role="manager", is_active="0", username="hacker", user_id=str(bob_id), id=str(bob_id),
         must_change_password="1", created_at="2000-01-01")
    alice, bob = get_user(app, "alice"), get_user(app, "bob")
    assert (alice.role, alice.is_active, alice.username, alice.must_change_password) == ("employee", True, "alice", False)
    assert alice.full_name == "Pat Morgan"       # only her own record changed...
    assert bob.full_name is None and bob.email is None  # ...not bob's
    assert client.post(f"/profile/{bob_id}/edit", data={"full_name": "x"}).status_code == 404


@pytest.mark.parametrize("fields,message", [
    ({"full_name": "  "}, "Full name is required"),
    ({"full_name": "x" * 101}, "100 characters or fewer"),
    ({"email": ""}, "Email is required"),
    ({"email": "nope"}, "valid email"),
    ({"email": "taken@example.com"}, "already uses that email"),
    ({"email": "TAKEN@example.com"}, "already uses that email"),
])
def test_invalid_profile_edits_keep_what_was_typed(app, client, fields, message):
    set_details(app, "bob", email="taken@example.com")
    login(client, "alice")
    response = edit(client, **fields)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and message in page
    typed = fields.get("email", "pat@example.com")
    if typed.strip():
        assert f'value="{typed}"' in page or f'value="{typed.lower()}"' in page  # input kept
    assert get_user(app, "alice").full_name is None


def test_profile_edit_requires_login(client):
    assert client.get("/profile/edit").status_code == 302
    assert edit(client).status_code == 302


# ---------------------------------------------------------------------------
# Change password from the profile
# ---------------------------------------------------------------------------

def test_change_password_from_profile_returns_there(app, client):
    from conftest import PASSWORD
    other_device = app.test_client()
    login(other_device, "boss")
    login(client, "boss")
    page = client.get("/profile").get_data(as_text=True)
    assert 'href="/change-password?next=/profile"' in page

    response = client.post("/change-password", data={
        "current_password": PASSWORD, "new_password": "Inventory2026", "confirm_password": "Inventory2026",
        "next": "/profile",
    })
    assert response.headers["Location"].endswith("/profile")
    assert "Your password was changed" in client.get("/profile").get_data(as_text=True)
    boss = get_user(app, "boss")
    assert boss.check_password("Inventory2026") and boss.password_hash != "Inventory2026"
    assert other_device.get("/overview").status_code == 302   # other session logged out
    with app.app_context():
        entry = ActivityLog.query.filter_by(action="Changed password").one()
        assert "Inventory2026" not in entry.action and boss.password_hash not in entry.action


def test_change_password_requires_the_current_password(app, client):
    login(client, "alice")
    for current in ["", "WrongPassword9"]:
        response = client.post("/change-password", data={
            "current_password": current, "new_password": "Inventory2026", "confirm_password": "Inventory2026",
        })
        assert response.status_code == 400 and "current password is incorrect" in response.get_data(as_text=True)
    assert not get_user(app, "alice").check_password("Inventory2026")


def test_change_password_never_redirects_off_site(app, client):
    from conftest import PASSWORD
    login(client, "alice")
    response = client.post("/change-password", data={
        "current_password": PASSWORD, "new_password": "Inventory2026", "confirm_password": "Inventory2026",
        "next": "https://evil.example/phish",
    })
    assert response.headers["Location"].endswith("/employee")
