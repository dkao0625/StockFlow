"""
Activity log pages.

  /activity            (managers) every action, with search, filters, pages
  /activity/export.csv (managers) the same filtered list as a CSV download
  /my-activity         (anyone)   only your own actions

Activity rows are read-only: nothing here (or anywhere) edits or deletes them.
"""

from datetime import datetime, time, timedelta, timezone

from flask import Blueprint, g, render_template, request

from auth import login_required, manager_required
from csv_export import MAX_EXPORT_ROWS, csv_response
from models import ACTIVITY_CATEGORIES, ROLE_EMPLOYEE, ROLE_MANAGER, ActivityLog, Product, User, db

activity_bp = Blueprint("activity", __name__)

PER_PAGE = 50
# My Activity is about inventory work, so logins and account events are left out.
NOT_INVENTORY_WORK = ["login", "account"]
INVENTORY_CATEGORIES = {k: v for k, v in ACTIVITY_CATEGORIES.items() if k not in NOT_INVENTORY_WORK}


def parse_day(text):
    """'2026-11-17' -> date, or None if blank/invalid."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def read_filters():
    filters = {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", ""),
        "user": request.args.get("user", type=int),
        "role": request.args.get("role", ""),
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
    }
    if filters["category"] not in ACTIVITY_CATEGORIES:
        filters["category"] = ""
    if filters["role"] not in (ROLE_EMPLOYEE, ROLE_MANAGER):
        filters["role"] = ""
    # Keep only valid dates (so the form shows what was actually applied).
    for key in ("date_from", "date_to"):
        if parse_day(filters[key]) is None:
            filters[key] = ""
    return filters


def filtered_query(filters, only_user_id=None):
    """ActivityLog query with every filter applied, newest first."""
    query = ActivityLog.query.outerjoin(User, ActivityLog.actor_id == User.id).outerjoin(
        Product, ActivityLog.product_id == Product.id
    )
    if only_user_id is not None:
        query = query.filter(
            ActivityLog.actor_id == only_user_id,
            db.or_(ActivityLog.category.is_(None), ActivityLog.category.notin_(NOT_INVENTORY_WORK)),
        )
    if filters["q"]:
        # Parameterized ILIKE (safe from SQL injection).
        pattern = f"%{filters['q']}%"
        query = query.filter(db.or_(
            ActivityLog.action.ilike(pattern),
            User.username.ilike(pattern),
            Product.sku.ilike(pattern),
            Product.name.ilike(pattern),
        ))
    if filters["category"]:
        query = query.filter(ActivityLog.category == filters["category"])
    if filters.get("user"):
        query = query.filter(ActivityLog.actor_id == filters["user"])
    if filters.get("role"):
        query = query.filter(ActivityLog.actor_role == filters["role"])
    # Dates are whole days in UTC (the same time zone shown on the page).
    start = parse_day(filters["date_from"])
    if start:
        query = query.filter(ActivityLog.created_at >= datetime.combine(start, time.min, timezone.utc))
    end = parse_day(filters["date_to"])
    if end:
        query = query.filter(
            ActivityLog.created_at < datetime.combine(end + timedelta(days=1), time.min, timezone.utc)
        )
    return query.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())


def url_args(filters):
    """Filters as URL parameters (blank ones left out)."""
    return {key: value for key, value in filters.items() if value}


@activity_bp.route("/activity")
@manager_required
def activity_log():
    filters = read_filters()
    page = filtered_query(filters).paginate(
        page=request.args.get("page", 1, type=int), per_page=PER_PAGE, error_out=False
    )
    return render_template(
        "activity.html",
        entries=page.items,
        page=page,
        filters=filters,
        args=url_args(filters),
        categories=ACTIVITY_CATEGORIES,
        users=User.query.order_by(User.username).all(),
        mine=False,
    )


@activity_bp.route("/activity/export.csv")
@manager_required
def export_activity():
    entries = filtered_query(read_filters()).limit(MAX_EXPORT_ROWS).all()
    rows = (
        [
            entry.created_at,
            entry.actor.username if entry.actor else "",
            entry.actor_role or "",
            entry.category_label,
            entry.product.sku if entry.product else "",
            entry.product.name if entry.product else "",
            entry.target_user.username if entry.target_user else "",
            entry.quantity_change,
            entry.request_id,
            entry.action,
        ]
        for entry in entries
    )
    header = ["Timestamp (UTC)", "User", "Role", "Action", "Product SKU", "Product",
              "Affected account", "Quantity change", "Request ID", "Description"]
    return csv_response("stockflow-activity", header, rows)


@activity_bp.route("/my-activity")
@login_required
def my_activity():
    """Your own inventory actions only (the user filter is fixed to you)."""
    filters = read_filters()
    filters["user"] = None
    filters["role"] = ""
    if filters["category"] not in INVENTORY_CATEGORIES:
        filters["category"] = ""
    page = filtered_query(filters, only_user_id=g.user.id).paginate(
        page=request.args.get("page", 1, type=int), per_page=PER_PAGE, error_out=False
    )
    return render_template(
        "activity.html",
        entries=page.items,
        page=page,
        filters=filters,
        args=url_args(filters),
        categories=INVENTORY_CATEGORIES,
        users=[],
        mine=True,
    )
