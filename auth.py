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
