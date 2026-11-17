"""
Authentication: register, login, logout, and permission decorators.

How login works (session-based authentication):
  1. The user submits a username and password.
  2. We look up the user and compare the password with the stored hash.
  3. If correct, we save the user's id in Flask's `session`. Flask signs
     the session cookie with SECRET_KEY so users cannot tamper with it.
  4. On every request, `load_logged_in_user` reads the session and puts
     the User object in `g.user`.
"""

import re
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from models import ROLE_EMPLOYEE, ROLE_MANAGER, User, db, log_activity, utc_now

auth_bp = Blueprint("auth", __name__)

USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,30}$")
MIN_PASSWORD_LENGTH = 8           # used by the `flask create-manager` command
NEW_PASSWORD_MIN_LENGTH = 10      # temporary passwords and password changes
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254
MAX_FULL_NAME_LENGTH = 100


def password_problem(password, username=""):
    """
    Why this password is too weak, or None if it's fine.
    Rules: at least 10 characters, with a letter and a number, and not
    containing the username.
    """
    if len(password) < NEW_PASSWORD_MIN_LENGTH:
        return f"Password must be at least {NEW_PASSWORD_MIN_LENGTH} characters."
    if len(password) > 128:
        return "Password must be 128 characters or fewer."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        return "Password must contain at least one letter and one number."
    if username and username.lower() in password.lower():
        return "Password can't contain the username."
    return None


# ---------------------------------------------------------------------------
# Loading the current user
# ---------------------------------------------------------------------------

@auth_bp.before_app_request
def load_logged_in_user():
    """Runs before every request and sets g.user (or None)."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return

    user = db.session.get(User, user_id)

    # Reject the session if the user no longer exists, if they logged out
    # after this session was created (session_version changed), or if a
    # manager deactivated the account -- checked on EVERY request, so a
    # deactivated user is locked out immediately, even mid-session.
    if (
        user is None
        or session.get("session_version") != user.session_version
        or not user.is_active
    ):
        session.clear()
        return

    g.user = user

    # Temporary password (new account, or reset by a manager): until the user
    # picks their own, the ONLY things they can do are change it or log out.
    # This runs before every view on the server, so typing another URL or
    # sending a request directly can't skip it.
    if user.must_change_password and request.endpoint not in PASSWORD_CHANGE_ALLOWED:
        if request.method == "GET":
            flash("Please choose a new password before continuing.", "warning")
        return redirect(url_for("auth.change_password"))


# Pages a user with a temporary password may still use.
PASSWORD_CHANGE_ALLOWED = {"auth.change_password", "auth.logout", "static"}


# ---------------------------------------------------------------------------
# Permission decorators -- these protect routes on the SERVER side.
# Hiding a button in HTML is not enough, because anyone can send a
# request directly to a URL.
# ---------------------------------------------------------------------------

def login_required(view):
    """Redirect to the login page unless someone is logged in."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped_view


def manager_required(view):
    """Allow only managers. Employees get HTTP 403 Forbidden."""

    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        if g.user.role != ROLE_MANAGER:
            abort(403)
        return view(*args, **kwargs)

    return wrapped_view


def employee_required(view):
    """Allow only employees (managers don't submit damage reports)."""

    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        if g.user.role != ROLE_EMPLOYEE:
            abort(403)
        return view(*args, **kwargs)

    return wrapped_view


def home_for(user):
    """The page each role lands on after logging in."""
    if user.role == ROLE_MANAGER:
        return url_for("manager.overview")
    return url_for("inventory.employee_dashboard")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# There is NO public registration. Accounts are created by managers on the
# Employees page (employee accounts) or with `flask create-manager` (manager
# accounts, needs database access). The old /register URL no longer exists,
# so visiting or posting to it returns 404.


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(home_for(g.user))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        # Log in with either the username or the email address.
        user = User.query.filter(db.or_(User.username == username, User.email == username)).first()

        # Same message for "no such user" and "wrong password" so attackers
        # can't discover which usernames exist.
        if user is None or not user.check_password(password):
            flash("Invalid username or password.", "error")
            return render_template("login.html", username=username), 401
        # Only said AFTER the correct password, so it can't be used to find
        # out which usernames exist.
        if not user.is_active:
            flash("This account has been deactivated. Please ask a manager.", "error")
            return render_template("login.html", username=username), 403

        # Start a fresh session (prevents "session fixation" attacks).
        session.clear()
        session["user_id"] = user.id
        session["session_version"] = user.session_version
        session.permanent = True  # expires after PERMANENT_SESSION_LIFETIME

        user.last_login_at = utc_now()
        log_activity(user.id, "Logged in", category="login")
        db.session.commit()
        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        return redirect(home_for(user))

    return render_template("login.html", username="")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    # Bumping session_version makes any copy of the old session cookie
    # useless, not just the one in this browser.
    g.user.session_version += 1
    log_activity(g.user.id, "Logged out", category="login")
    db.session.commit()

    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """
    Choose a new password. Required after logging in with a temporary
    password (see load_logged_in_user); any user can also use it any time.
    """
    first_time = g.user.must_change_password
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        # Asking for the current password means someone who finds an
        # unattended logged-in screen can't take over the account.
        if not g.user.check_password(current):
            error = "Your current password is incorrect."
        elif new != confirm:
            error = "The new passwords don't match."
        elif g.user.check_password(new):
            error = "Choose a password different from your current one."
        else:
            error = password_problem(new, g.user.username)
        if error:
            flash(error, "error")
            return render_template("change_password.html", first_time=first_time,
                                   next_url=back_to_page_or("")), 400

        g.user.set_password(new)  # stored hashed
        g.user.must_change_password = False
        # Log out every OTHER session (e.g. on another computer), but keep
        # this one working by giving it the new version number.
        g.user.session_version += 1
        session["session_version"] = g.user.session_version
        log_activity(
            g.user.id,
            "Changed temporary password (first login)" if first_time else "Changed password",
            category="account", target_user_id=g.user.id,
        )
        db.session.commit()
        flash("Your password was changed. You've been logged out on other devices.", "success")
        if first_time:
            return redirect(home_for(g.user))
        return redirect(back_to_page_or(home_for(g.user)))

    return render_template("change_password.html", first_time=first_time, next_url=back_to_page_or(""))


def back_to_page_or(default):
    """The ?next= page to return to, but only a page on THIS site (never another website)."""
    next_url = request.values.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url:
        return next_url
    return default
