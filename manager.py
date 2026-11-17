"""
Manager-only pages.

Overview (/overview): the manager's home page. Summary cards, the newest
pending requests, stock alerts, and recent inventory activity -- all read
live from the database, nothing hard-coded.
"""

from decimal import Decimal

from flask import Blueprint, render_template

from auth import manager_required
from models import (
    STATUS_PENDING,
    STOCK_LOW,
    STOCK_OUT,
    ActivityLog,
    InventoryRequest,
    Product,
)

manager_bp = Blueprint("manager", __name__)

OVERVIEW_PENDING_LIMIT = 5   # newest pending requests shown on the Overview
OVERVIEW_ALERT_LIMIT = 8     # stock alerts shown before "View all"
OVERVIEW_ACTIVITY_LIMIT = 10  # newest inventory actions shown


def overview_stats(products):
    """
    Numbers for the six Overview cards, from the list of ACTIVE products.

    Inventory value = quantity x unit cost, only for products that have a
    unit cost recorded. Products that are in stock but have no cost are
    counted separately so the page can say the total is incomplete.
    """
    inventory_value = Decimal("0.00")
    missing_cost = 0
    for product in products:
        if product.unit_cost is None:
            if product.quantity > 0:
                missing_cost += 1  # units we can't put a value on
        else:
            inventory_value += product.quantity * product.unit_cost

    return {
        "total_products": len(products),
        "total_units": sum(p.quantity for p in products),
        "pending_requests": InventoryRequest.query.filter_by(status=STATUS_PENDING).count(),
        "low_stock": sum(1 for p in products if p.stock_status == STOCK_LOW),
        "out_of_stock": sum(1 for p in products if p.stock_status == STOCK_OUT),
        "inventory_value": inventory_value,
        "missing_cost": missing_cost,
    }


def stock_alert_products(products):
    """Active products that are Low or Out of Stock: out of stock first, then lowest quantity."""
    alerts = [p for p in products if p.stock_status in (STOCK_LOW, STOCK_OUT)]
    alerts.sort(key=lambda p: (p.quantity != 0, p.quantity, p.name.lower()))
    return alerts


@manager_bp.route("/overview")
@manager_required
def overview():
    products = Product.query.filter_by(is_archived=False).order_by(Product.name).all()
    alerts = stock_alert_products(products)

    pending_requests = (
        InventoryRequest.query.filter_by(status=STATUS_PENDING)
        .order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(OVERVIEW_PENDING_LIMIT)
        .all()
    )
    # "Inventory actions" = log entries about a product (edits, stock
    # changes, requests, approvals, archiving). Logins/logouts are left out.
    recent_activity = (
        ActivityLog.query.filter(ActivityLog.product_id.isnot(None))
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(OVERVIEW_ACTIVITY_LIMIT)
        .all()
    )

    return render_template(
        "overview.html",
        stats=overview_stats(products),
        pending_requests=pending_requests,
        alerts=alerts[:OVERVIEW_ALERT_LIMIT],
        alert_total=len(alerts),
        recent_activity=recent_activity,
    )
