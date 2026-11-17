"""
Stock Alerts: products that are Low Stock or Out of Stock.

  quantity == 0                  -> Out of Stock
  0 < quantity <= reorder_level  -> Low Stock
  quantity > reorder_level       -> In Stock (not shown here)

Everyone logged in can view this page. Only managers can change a product's
reorder level and target stock level (the target is used to suggest how
many units to reorder).
"""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import login_required, manager_required
from csv_export import csv_response
from models import Product, db, log_activity
from products import parse_whole_number, safe_next_url
from restock import on_order_by_product
from stock import MAX_STOCK_QUANTITY

alerts_bp = Blueprint("alerts", __name__, url_prefix="/alerts")

# ?sort=... value -> (label, ORDER BY columns)
SORT_OPTIONS = {
    "quantity": ("Quantity: lowest first", [Product.quantity, Product.name]),
    "quantity_desc": ("Quantity: highest first", [Product.quantity.desc(), Product.name]),
    "reorder": ("Reorder level: lowest first", [Product.reorder_level, Product.name]),
    "reorder_desc": ("Reorder level: highest first", [Product.reorder_level.desc(), Product.name]),
}
STATUS_FILTERS = {"low": "Low Stock", "out": "Out of Stock"}


def alert_query():
    """Active products at or below their reorder level (this includes everything at 0)."""
    return Product.query.filter(
        Product.is_archived.is_(False),
        Product.quantity <= Product.reorder_level,
    )


def alert_filters():
    filters = {
        "status": request.args.get("status", ""),
        "q": request.args.get("q", "").strip(),
        "sort": request.args.get("sort", "quantity"),
    }
    if filters["sort"] not in SORT_OPTIONS:
        filters["sort"] = "quantity"
    return filters


def filtered_alerts(filters):
    """Alert products matching the page filters, sorted (used by the page and its CSV export)."""
    query = alert_query()
    if filters["status"] == "low":
        query = query.filter(Product.quantity > 0)
    elif filters["status"] == "out":
        query = query.filter(Product.quantity == 0)
    if filters["q"]:
        # Parameterized ILIKE (case-insensitive), safe from SQL injection.
        pattern = f"%{filters['q']}%"
        query = query.filter(
            db.or_(
                Product.name.ilike(pattern),
                Product.sku.ilike(pattern),
                Product.location.ilike(pattern),
                Product.supplier.ilike(pattern),
            )
        )
    return query.order_by(*SORT_OPTIONS[filters["sort"]][1]).all()


@alerts_bp.route("")
@login_required
def stock_alerts():
    filters = alert_filters()
    query = alert_query()
    counts = {
        "all": query.count(),
        "low": query.filter(Product.quantity > 0).count(),
        "out": query.filter(Product.quantity == 0).count(),
    }
    products = filtered_alerts(filters)

    return render_template(
        "alerts.html",
        products=products,
        filters=filters,
        counts=counts,
        sort_options=SORT_OPTIONS,
        status_filters=STATUS_FILTERS,
        on_order=on_order_by_product(),
    )


@alerts_bp.route("/<int:product_id>/levels", methods=["POST"])
@manager_required
def update_levels(product_id):
    """Manager sets a product's reorder level and (optional) target stock level."""
    back = safe_next_url(url_for("alerts.stock_alerts"))
    product = db.get_or_404(Product, product_id)
    if product.is_archived:
        flash(f"{product.name} is archived.", "error")
        return redirect(back)

    reorder_level, error = parse_whole_number(
        request.form.get("reorder_level", "").strip(), "Reorder level", MAX_STOCK_QUANTITY
    )
    target_text = request.form.get("target_stock_level", "").strip()
    target = None
    if not error and target_text:  # blank target = "no target"
        target, error = parse_whole_number(target_text, "Target stock level", MAX_STOCK_QUANTITY)
        if not error and target <= reorder_level:
            error = "Target stock level must be higher than the reorder level."
    if error:
        flash(f"{product.name}: {error}", "error")
        return redirect(back)

    def show(value):
        return "none" if value is None else value

    changes = []
    if reorder_level != product.reorder_level:
        changes.append(f"reorder level {product.reorder_level} → {reorder_level}")
    if target != product.target_stock_level:
        changes.append(f"target {show(product.target_stock_level)} → {show(target)}")
    if not changes:
        flash("Nothing changed.", "warning")
        return redirect(back)

    product.reorder_level = reorder_level
    product.target_stock_level = target
    log_activity(g.user.id, f"Changed stock levels for {product.sku}: {', '.join(changes)}", product.id,
                 category="product_edited")
    db.session.commit()
    flash(f"{product.name}: {', '.join(changes)}.", "success")
    return redirect(back)


@alerts_bp.route("/export.csv")
@manager_required
def export_alerts():
    products = filtered_alerts(alert_filters())
    on_order = on_order_by_product()
    header = ["SKU", "Name", "Category", "Available", "Unit", "Reorder level", "Target stock level",
              "Suggested reorder", "On order", "Location", "Supplier", "Status"]
    rows = (
        [
            p.sku, p.name, p.category, p.quantity, p.unit, p.reorder_level, p.target_stock_level,
            p.suggested_reorder_quantity, on_order.get(p.id), p.location, p.supplier, p.stock_status,
        ]
        for p in products
    )
    return csv_response("stockflow-stock-alerts", header, rows)
