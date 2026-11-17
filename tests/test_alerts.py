"""Stock Alerts page: which products appear, filters, and manager-only level settings."""

from conftest import login, product_id
from models import ActivityLog, Product, db

# Fixture products: Widget T-1 qty 10 / reorder 3 (In Stock), Gadget T-2 qty 2 / reorder 5 (Low).


def add_products(app):
    with app.app_context():
        db.session.add_all([
            Product(sku="T-3", name="Empty bin", quantity=0, reorder_level=4, supplier="Acme Supply"),
            Product(sku="T-4", name="Hidden box", quantity=0, reorder_level=4, is_archived=True),
            Product(sku="T-5", name="Edge case", quantity=7, reorder_level=7, target_stock_level=20),
        ])
        db.session.commit()


def html(client, url="/alerts"):
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def table(page):
    return page.split("<tbody>")[1].split("</tbody>")[0]


def test_alerts_require_login(client):
    assert client.get("/alerts").status_code == 302


def test_lists_only_low_and_out_of_stock_active_products(app, client):
    add_products(app)
    login(client, "alice")  # employees can view alerts
    rows = table(html(client))
    assert "Gadget" in rows and "Empty bin" in rows
    assert "Edge case" in rows        # quantity == reorder level counts as Low Stock
    assert "Widget" not in rows       # In Stock
    assert "Hidden box" not in rows   # archived


def test_status_filters_and_search(app, client):
    add_products(app)
    login(client, "boss")
    low = table(html(client, "/alerts?status=low"))
    assert "Gadget" in low and "Empty bin" not in low
    out = table(html(client, "/alerts?status=out"))
    assert "Empty bin" in out and "Gadget" not in out
    found = table(html(client, "/alerts?q=acme"))  # matches supplier
    assert "Empty bin" in found and "Gadget" not in found


def test_sorting(app, client):
    add_products(app)
    login(client, "boss")
    by_qty = table(html(client, "/alerts?sort=quantity"))
    assert by_qty.index("Empty bin") < by_qty.index("Gadget") < by_qty.index("Edge case")
    by_qty_desc = table(html(client, "/alerts?sort=quantity_desc"))
    assert by_qty_desc.index("Edge case") < by_qty_desc.index("Gadget") < by_qty_desc.index("Empty bin")
    by_reorder = table(html(client, "/alerts?sort=reorder"))
    assert by_reorder.index("Empty bin") < by_reorder.index("Gadget") < by_reorder.index("Edge case")


def test_suggested_reorder_quantity(app, client):
    add_products(app)
    login(client, "boss")
    rows = table(html(client))
    edge_row = rows.split("Edge case")[1].split("</tr>")[0]
    assert "<strong>13</strong>" in edge_row          # target 20 - quantity 7
    gadget_row = rows.split("Gadget")[1].split("</tr>")[0]
    assert "Enter manually" in gadget_row             # no target set


def test_employee_sees_alerts_read_only(app, client):
    login(client, "alice")
    assert "Set levels" not in html(client)
    pid = product_id(app, "T-2")
    response = client.post(f"/alerts/{pid}/levels", data={"reorder_level": 1})
    assert response.status_code == 403
    with app.app_context():
        assert db.session.get(Product, pid).reorder_level == 5


def test_manager_sets_levels_and_it_is_logged(app, client):
    pid = product_id(app, "T-2")
    login(client, "boss")
    assert "Set levels" in html(client)
    client.post(f"/alerts/{pid}/levels", data={"reorder_level": 4, "target_stock_level": 12})
    with app.app_context():
        product = db.session.get(Product, pid)
        assert (product.reorder_level, product.target_stock_level) == (4, 12)
        assert product.suggested_reorder_quantity == 10
        entry = ActivityLog.query.filter(ActivityLog.action.like("Changed stock levels%")).one()
        assert "reorder level 5 → 4" in entry.action and "target none → 12" in entry.action

    # A blank target clears it.
    client.post(f"/alerts/{pid}/levels", data={"reorder_level": 4, "target_stock_level": ""})
    with app.app_context():
        assert db.session.get(Product, pid).target_stock_level is None


def test_invalid_levels_are_rejected(app, client):
    pid = product_id(app, "T-2")
    login(client, "boss")
    for bad in [
        {"reorder_level": "-1"},
        {"reorder_level": ""},
        {"reorder_level": "abc"},
        {"reorder_level": "5", "target_stock_level": "5"},   # target must be above reorder level
        {"reorder_level": "5", "target_stock_level": "2.5"},
    ]:
        client.post(f"/alerts/{pid}/levels", data=bad)
    with app.app_context():
        product = db.session.get(Product, pid)
        assert (product.reorder_level, product.target_stock_level) == (5, None)
