"""
Employee management (managers only).

Managers can see every account, open a profile (requests + activity), and
activate or deactivate accounts. They can NOT see or set passwords, and they
can NOT change anyone's role here: managers are only created with the
`flask create-manager` command, which needs direct database access.

Safety rules (checked on the server):
  - you can't deactivate your own account
  - the last active manager can't be deactivated
  - a deactivated user is logged out everywhere at once
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import manager_required
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
        query = query.filter(User.username.ilike(f"%{filters['q']}%"))
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
