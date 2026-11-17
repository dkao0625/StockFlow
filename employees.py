"""
Employee management (managers only).

Managers can see every account, open a profile (requests + activity),
create EMPLOYEE accounts, and activate or deactivate accounts. They can NOT
see anyone's password, and they can NOT create managers or change roles
here: managers are only created with the `flask create-manager` command,
which needs direct database access. There is no public sign-up page.

Safety rules (checked on the server):
  - you can't deactivate your own account
  - the last active manager can't be deactivated
  - a deactivated user is logged out everywhere at once
"""

import secrets
import string

from flask import Blueprint, abort, flash, g, make_response, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from auth import (
    EMAIL_PATTERN,
    MAX_EMAIL_LENGTH,
    MAX_FULL_NAME_LENGTH,
    USERNAME_PATTERN,
    manager_required,
    password_problem,
)
from models import (
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    STATUS_PENDING,
    ActivityLog,
    InventoryRequest,
    User,
    db,
    log_activity,
)
from products import safe_next_url

employees_bp = Blueprint("employees", __name__, url_prefix="/employees")

PROFILE_LIMIT = 25  # requests / activity rows shown on a profile

# Letters and digits that can't be confused when read aloud or typed
# (no 0/O, 1/l/I).
PASSWORD_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
GENERATED_PASSWORD_LENGTH = 12


def generate_temporary_password():
    """A random password from the OS's secure random source (secrets module)."""
    while True:
        password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH))
        if password_problem(password) is None:  # has a letter and a number
            return password


def read_account_form(form, existing_user=None):
    """
    Validate full name, username, and email. Returns (values, errors).
    Uniqueness is checked here for a friendly message, AND enforced by the
    database's UNIQUE constraints (in case two managers submit at once).
    """
    values = {
        "full_name": form.get("full_name", "").strip(),
        "username": form.get("username", "").strip().lower(),
        "email": form.get("email", "").strip().lower(),
    }
    errors = {}
    if not values["full_name"]:
        errors["full_name"] = "Full name is required."
    elif len(values["full_name"]) > MAX_FULL_NAME_LENGTH:
        errors["full_name"] = f"Full name must be {MAX_FULL_NAME_LENGTH} characters or fewer."

    if not USERNAME_PATTERN.match(values["username"]):
        errors["username"] = "Username must be 3-30 characters: letters, numbers, or underscores."
    else:
        taken = User.query.filter(User.username == values["username"])
        if existing_user is not None:
            taken = taken.filter(User.id != existing_user.id)
        if taken.first():
            errors["username"] = "That username is already taken."

    if not values["email"]:
        errors["email"] = "Email is required."
    elif len(values["email"]) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(values["email"]):
        errors["email"] = "Enter a valid email address."
    else:
        taken = User.query.filter(User.email == values["email"])
        if existing_user is not None:
            taken = taken.filter(User.id != existing_user.id)
        if taken.first():
            errors["email"] = "Another account already uses that email."
    return values, errors


def no_store(response):
    """Tell the browser not to cache a page (used for pages showing a temporary password)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def request_counts():
    """{user_id: number of requests submitted}"""
    rows = (
        db.session.query(InventoryRequest.employee_id, db.func.count())
        .group_by(InventoryRequest.employee_id)
        .all()
    )
    return dict(rows)


def last_activity_times():
    """{user_id: time of their most recent logged action}"""
    rows = (
        db.session.query(ActivityLog.actor_id, db.func.max(ActivityLog.created_at))
        .filter(ActivityLog.actor_id.isnot(None))
        .group_by(ActivityLog.actor_id)
        .all()
    )
    return dict(rows)


@employees_bp.route("")
@manager_required
def employee_list():
    filters = {
        "q": request.args.get("q", "").strip().lower(),
        "role": request.args.get("role", ""),
        "status": request.args.get("status", ""),
    }
    query = User.query
    if filters["q"]:
        pattern = f"%{filters['q']}%"
        query = query.filter(db.or_(
            User.username.ilike(pattern), User.full_name.ilike(pattern), User.email.ilike(pattern)
        ))
    if filters["role"] in (ROLE_EMPLOYEE, ROLE_MANAGER):
        query = query.filter(User.role == filters["role"])
    if filters["status"] == "active":
        query = query.filter(User.is_active.is_(True))
    elif filters["status"] == "deactivated":
        query = query.filter(User.is_active.is_(False))
    users = query.order_by(User.role.desc(), User.username).all()  # employees first

    return render_template(
        "employees.html",
        users=users,
        filters=filters,
        request_counts=request_counts(),
        last_activity=last_activity_times(),
    )


@employees_bp.route("/new", methods=["GET", "POST"])
@manager_required
def new_employee():
    """
    Create an EMPLOYEE account. The role is always set here on the server;
    any "role" sent with the form is ignored. The new employee must change
    the temporary password the first time they log in.
    """
    if request.method == "GET":
        return render_template("employee_form.html", values={}, errors={}, password_choice="generate")

    values, errors = read_account_form(request.form)
    password_choice = request.form.get("password_choice", "generate")
    if password_choice == "manual":
        temporary_password = request.form.get("temporary_password", "")
        problem = password_problem(temporary_password, values["username"])
        if problem:
            errors["temporary_password"] = problem
    else:
        password_choice = "generate"
        temporary_password = generate_temporary_password()
    if errors:
        return render_template("employee_form.html", values=values, errors=errors,
                               password_choice=password_choice), 400

    employee = User(
        username=values["username"],
        email=values["email"],
        full_name=values["full_name"],
        role=ROLE_EMPLOYEE,          # always an employee
        is_active=True,
        must_change_password=True,   # temporary password -> change on first login
    )
    employee.set_password(temporary_password)  # only the hash is stored
    try:
        db.session.add(employee)
        db.session.flush()  # the UNIQUE constraints are checked here
        log_activity(
            g.user.id, f"Created employee account '{employee.username}'",
            category="account", target_user_id=employee.id,
        )
        db.session.commit()
    except IntegrityError:
        # Someone else created the same username or email a moment ago.
        db.session.rollback()
        errors["username"] = "That username or email was just taken. Choose another."
        return render_template("employee_form.html", values=values, errors=errors,
                               password_choice=password_choice), 400

    # Show a GENERATED password exactly once, on this page. It's not put in a
    # flash message (those are stored in the session cookie) or in any log.
    return no_store(make_response(render_template(
        "employee_created.html",
        employee=employee,
        temporary_password=temporary_password if password_choice == "generate" else None,
    )))


@employees_bp.route("/<int:user_id>")
@manager_required
def profile(user_id):
    user = db.get_or_404(User, user_id)
    requests_by_status = dict(
        db.session.query(InventoryRequest.status, db.func.count())
        .filter(InventoryRequest.employee_id == user.id)
        .group_by(InventoryRequest.status)
        .all()
    )
    recent_requests = (
        InventoryRequest.query.filter_by(employee_id=user.id)
        .order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(PROFILE_LIMIT)
        .all()
    )
    recent_activity = (
        ActivityLog.query.filter(
            db.or_(ActivityLog.actor_id == user.id, ActivityLog.target_user_id == user.id)
        )
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(PROFILE_LIMIT)
        .all()
    )
    return render_template(
        "employee_profile.html",
        user=user,
        requests_by_status=requests_by_status,
        total_requests=sum(requests_by_status.values()),
        pending_requests=requests_by_status.get(STATUS_PENDING, 0),
        recent_requests=recent_requests,
        recent_activity=recent_activity,
        last_activity=last_activity_times().get(user.id),
    )


def lock_active_managers():
    """
    Lock every active manager row (SELECT ... FOR UPDATE, in id order).

    Two managers deactivating each other at the same moment both have to
    wait for this lock, so the second one sees the first one's change and
    the "last active manager" rule can't be bypassed.
    """
    return db.session.execute(
        db.select(User)
        .where(User.role == ROLE_MANAGER, User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()


@employees_bp.route("/<int:user_id>/deactivate", methods=["POST"])
@manager_required
def deactivate(user_id):
    back = safe_next_url(url_for("employees.profile", user_id=user_id))
    if user_id == g.user.id:
        flash("You can't deactivate your own account.", "error")
        return redirect(back)

    try:
        active_managers = lock_active_managers()
        target = db.session.execute(
            db.select(User).where(User.id == user_id).with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if target is None:
            db.session.rollback()
            abort(404)
        if not target.is_active:
            db.session.rollback()
            flash(f"{target.username} is already deactivated.", "warning")
            return redirect(back)
        if target.is_manager and len(active_managers) <= 1:
            db.session.rollback()
            flash("You can't deactivate the last active manager.", "error")
            return redirect(back)

        target.is_active = False
        # Also invalidate every session they have open right now.
        target.session_version += 1
        log_activity(
            g.user.id, f"Deactivated {target.role} account '{target.username}'",
            category="account", target_user_id=target.id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"{target.username} was deactivated and logged out. Their history is kept.", "success")
    return redirect(back)


@employees_bp.route("/<int:user_id>/activate", methods=["POST"])
@manager_required
def activate(user_id):
    back = safe_next_url(url_for("employees.profile", user_id=user_id))
    target = db.get_or_404(User, user_id)
    if target.is_active:
        flash(f"{target.username} is already active.", "warning")
        return redirect(back)
    target.is_active = True
    log_activity(
        g.user.id, f"Reactivated {target.role} account '{target.username}'",
        category="account", target_user_id=target.id,
    )
    db.session.commit()
    flash(f"{target.username} can log in again.", "success")
    return redirect(back)
