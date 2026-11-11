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

from models import ROLE_EMPLOYEE, ROLE_MANAGER, User, db, log_activity

auth_bp = Blueprint("auth", __name__)

USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,30}$")
MIN_PASSWORD_LENGTH = 8


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

    # Reject the session if the user no longer exists, or if they logged
    # out after this session was created (session_version changed).
    if user is None or session.get("session_version") != user.session_version:
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
        return url_for("inventory.manager_dashboard")
    return url_for("inventory.employee_dashboard")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(home_for(g.user))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        # Server-side validation (never trust the browser alone).
        error = None
        if not USERNAME_PATTERN.match(username):
            error = "Username must be 3-30 characters: letters, numbers, or underscores."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif User.query.filter_by(username=username).first() is not None:
            error = "That username is already taken."

        if error:
            flash(error, "error")
            return render_template("register.html", username=username), 400

        # Public registration ALWAYS creates an employee. Any "role" field
        # someone adds to the form is ignored. Managers are created with
        # the `flask create-manager` command instead.
        new_user = User(username=username, role=ROLE_EMPLOYEE)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.flush()  # assigns new_user.id so we can log it
        log_activity(new_user.id, f"Registered employee account '{username}'")
        db.session.commit()

        flash("Account created. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("register.html", username="")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(home_for(g.user))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        # Same message for "no such user" and "wrong password" so attackers
        # can't discover which usernames exist.
        if user is None or not user.check_password(password):
            flash("Invalid username or password.", "error")
            return render_template("login.html", username=username), 401

        # Start a fresh session (prevents "session fixation" attacks).
        session.clear()
        session["user_id"] = user.id
        session["session_version"] = user.session_version
        session.permanent = True  # expires after PERMANENT_SESSION_LIFETIME

        log_activity(user.id, "Logged in")
        db.session.commit()
        return redirect(home_for(user))

    return render_template("login.html", username="")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    # Bumping session_version makes any copy of the old session cookie
    # useless, not just the one in this browser.
    g.user.session_version += 1
    log_activity(g.user.id, "Logged out")
    db.session.commit()

    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
