"""
Reports (managers only).

Every number is calculated from the database each time the page loads:
  - Inventory summary: current active products (right now, not date-limited)
  - Inventory movements: stock_movements in the chosen date range
  - Employee requests: inventory_requests submitted in the chosen date range

No forecasting or predictions -- only what actually happened.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from flask import Blueprint, flash, render_template, request

from activity import parse_day
from auth import manager_required
from csv_export import MAX_EXPORT_ROWS, csv_response
from inventory import requests_csv
from manager import overview_stats
from models import (
    STATUS_APPROVED,
    STATUS_PARTIALLY_RECEIVED,
    STATUS_PENDING,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    InventoryRequest,
    Product,
    StockMovement,
    StockReceipt,
)

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

DEFAULT_DAYS = 30
DAMAGE_REASONS = ("Damaged", "Missing")
APPROVED_STATUSES = (STATUS_APPROVED, STATUS_PARTIALLY_RECEIVED, STATUS_RECEIVED)


def date_range():
    """
    (first_day, last_day) from ?date_from=...&date_to=..., both included.
    Default: the last 30 days, ending today (UTC).
    """
    today = datetime.now(timezone.utc).date()
    last_day = parse_day(request.args.get("date_to", "")) or today
    first_day = parse_day(request.args.get("date_from", "")) or (last_day - timedelta(days=DEFAULT_DAYS - 1))
    if first_day > last_day:
        flash("The start date was after the end date, so they were swapped.", "warning")
        first_day, last_day = last_day, first_day
    return first_day, last_day


def range_bounds(first_day, last_day):
    """UTC datetimes: start of first_day, and start of the day AFTER last_day."""
    start = datetime.combine(first_day, time.min, timezone.utc)
    end = datetime.combine(last_day + timedelta(days=1), time.min, timezone.utc)
    return start, end


def is_damage_related(movement):
    """A stock decrease caused by a Damaged/Missing report or adjustment."""
    if movement.quantity_change >= 0:
        return False
    if movement.request is not None:
        return movement.request.reason in DAMAGE_REASONS
    # Managers' direct bulk adjustments write the reason at the start of the note.
    return (movement.note or "").startswith(DAMAGE_REASONS)


def movements_in(start, end):
    return (
        StockMovement.query.filter(StockMovement.created_at >= start, StockMovement.created_at < end)
        .order_by(StockMovement.created_at, StockMovement.id)
        .all()
    )


def movement_report(start, end):
    movements = movements_in(start, end)
    by_day = defaultdict(lambda: {"added": 0, "removed": 0})
    totals = {"added": 0, "removed": 0, "damage": 0, "count": len(movements)}
    for movement in movements:
        day = movement.created_at.date()
        if movement.quantity_change > 0:
            totals["added"] += movement.quantity_change
            by_day[day]["added"] += movement.quantity_change
        else:
            totals["removed"] += -movement.quantity_change
            by_day[day]["removed"] += -movement.quantity_change
        if is_damage_related(movement):
            totals["damage"] += -movement.quantity_change

    totals["received"] = sum(
        receipt.quantity
        for receipt in StockReceipt.query.filter(
            StockReceipt.received_at >= start, StockReceipt.received_at < end
        )
    )
    totals["net"] = totals["added"] - totals["removed"]
    days = [{"day": day, **values} for day, values in sorted(by_day.items())]
    biggest = max([max(d["added"], d["removed"]) for d in days], default=0)
    return totals, days, biggest


def request_report(start, end):
    requests_in_range = InventoryRequest.query.filter(
        InventoryRequest.created_at >= start, InventoryRequest.created_at < end
    ).all()

    def summarize(items):
        return {
            "total": len(items),
            "pending": sum(r.status == STATUS_PENDING for r in items),
            "approved": sum(r.status in APPROVED_STATUSES for r in items),
            "rejected": sum(r.status == STATUS_REJECTED for r in items),
        }

    per_employee = defaultdict(list)
    per_type = defaultdict(int)
    for item in requests_in_range:
        per_employee[item.employee.username].append(item)
        per_type[item.type_label] += 1

    employees = sorted(
        ({"username": name, **summarize(items)} for name, items in per_employee.items()),
        key=lambda row: (-row["total"], row["username"]),
    )
    return summarize(requests_in_range), employees, dict(sorted(per_type.items(), key=lambda kv: -kv[1]))


@reports_bp.route("")
@manager_required
def reports():
    first_day, last_day = date_range()
    start, end = range_bounds(first_day, last_day)
    movement_totals, movement_days, biggest_day = movement_report(start, end)
    request_totals, employee_rows, type_counts = request_report(start, end)
    products = Product.query.filter_by(is_archived=False).all()

    return render_template(
        "reports.html",
        first_day=first_day,
        last_day=last_day,
        range_args={"date_from": first_day.isoformat(), "date_to": last_day.isoformat()},
        summary=overview_stats(products),
        movement_totals=movement_totals,
        movement_days=movement_days,
        biggest_day=biggest_day,
        request_totals=request_totals,
        employee_rows=employee_rows,
        type_counts=type_counts,
        biggest_employee=max([row["total"] for row in employee_rows], default=0),
    )


@reports_bp.route("/movements.csv")
@manager_required
def export_movements():
    first_day, last_day = date_range()
    start, end = range_bounds(first_day, last_day)
    movements = movements_in(start, end)[:MAX_EXPORT_ROWS]
    header = ["Date (UTC)", "Product SKU", "Product", "Quantity change", "Damage related",
              "Recorded by", "Request ID", "Note"]
    rows = (
        [
            m.created_at, m.product.sku, m.product.name, m.quantity_change, is_damage_related(m),
            m.actor.username if m.actor else None, m.request_id, m.note,
        ]
        for m in movements
    )
    return csv_response(f"stockflow-movements-{first_day}-to-{last_day}", header, rows)


@reports_bp.route("/requests.csv")
@manager_required
def export_requests():
    first_day, last_day = date_range()
    start, end = range_bounds(first_day, last_day)
    found = (
        InventoryRequest.query.filter(
            InventoryRequest.created_at >= start, InventoryRequest.created_at < end
        )
        .order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(MAX_EXPORT_ROWS)
        .all()
    )
    return requests_csv(found, f"stockflow-requests-{first_day}-to-{last_day}")
