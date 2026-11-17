"""`flask seed-products`: demo inventory that is safe to run more than once."""

import pytest

from conftest import login
from models import ActivityLog, InventoryRequest, Product, StockMovement, User, db
from seed_data import DEMO_PRODUCTS, seed_products


@pytest.fixture
def empty_inventory(app):
    """Start from no products (the shared fixture adds two test products)."""
    with app.app_context():
        Product.query.delete()
        db.session.commit()


def counts(app):
    with app.app_context():
        return (Product.query.count(), StockMovement.query.count(),
                ActivityLog.query.filter_by(category="product_created").count())


def test_seeding_an_empty_database_gives_the_expected_numbers(app, empty_inventory):
    with app.app_context():
        added, skipped = seed_products()
        assert (len(added), skipped) == (12, [])
        from inventory import active_products, inventory_stats
        assert inventory_stats(active_products()) == {
            "total_products": 12, "total_units": 888, "low_stock": 4, "out_of_stock": 1,
        }
        low = {p.sku for p in Product.query.all() if p.stock_status == "Low Stock"}
        assert low == {"SF-1002", "SF-1004", "SF-1008", "SF-1010"}
        assert Product.query.filter_by(sku="SF-1006").one().stock_status == "Out of Stock"
        # Every product has the extra details, with supported categories/units (DB checks them too).
        for product in Product.query.all():
            assert product.description and product.location and product.supplier
            assert product.unit_cost is not None and product.unit_cost > 0


def test_stock_history_matches_quantities(app, empty_inventory):
    with app.app_context():
        seed_products()
        movements = StockMovement.query.all()
        assert len(movements) == 11                       # gloves start at 0: no movement
        assert sum(m.quantity_change for m in movements) == 888
        assert all(m.note == "Starting stock (demo data)" and m.request_id is None for m in movements)
        for product in Product.query.all():
            history = sum(m.quantity_change for m in StockMovement.query.filter_by(product_id=product.id))
            assert history == product.quantity


def test_running_twice_adds_nothing_the_second_time(app, empty_inventory):
    with app.app_context():
        seed_products()
    first = counts(app)
    with app.app_context():
        added, skipped = seed_products()
    assert (added, len(skipped)) == ([], 12)
    assert counts(app) == first  # no duplicate products, movements, or log entries


def test_existing_products_are_never_changed(app, empty_inventory):
    with app.app_context():
        db.session.add(Product(sku="SF-1002", name="My own tape", quantity=999, reorder_level=1))
        db.session.commit()
        added, skipped = seed_products()
        assert skipped == ["SF-1002"] and len(added) == 11
        mine = Product.query.filter_by(sku="SF-1002").one()
        assert (mine.name, mine.quantity, mine.reorder_level) == ("My own tape", 999, 1)
        assert StockMovement.query.filter_by(product_id=mine.id).count() == 0


def test_failure_rolls_everything_back(app, empty_inventory, monkeypatch):
    import seed_data
    broken = DEMO_PRODUCTS[:3] + [dict(DEMO_PRODUCTS[3], category="Not a category")]
    monkeypatch.setattr(seed_data, "DEMO_PRODUCTS", broken)
    with app.app_context():
        with pytest.raises(Exception):
            seed_data.seed_products()
    assert counts(app) == (0, 0, 0)


def test_no_fake_users_or_requests(app, empty_inventory):
    with app.app_context():
        users_before = User.query.count()
        seed_products()
        assert User.query.count() == users_before
        assert InventoryRequest.query.count() == 0


def test_products_show_on_the_inventory_page(app, client, empty_inventory):
    with app.app_context():
        seed_products()
    login(client, "boss")
    page = client.get("/inventory").get_data(as_text=True)
    assert "Cardboard Shipping Box (Medium)" in page and "Packing Scissors" in page
    stats = page.split('<section class="stats">')[1].split("</section>")[0]
    for value in ["12", "888", "4", "1"]:
        assert f'<div class="stat-value">{value}</div>' in stats
    assert "Wireless Mouse" in client.get("/alerts").get_data(as_text=True)  # low stock alert


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def run(app, *args, answer=""):
    return app.test_cli_runner().invoke(args=["seed-products", *args], input=answer)


def test_cli_shows_the_database_and_asks_first(app, empty_inventory):
    result = run(app, answer="n\n")
    assert "Database:" in result.output and "Aborted" in result.output
    assert counts(app)[0] == 0
    result = run(app, answer="y\n")
    assert "Added 12 product(s); skipped 0" in result.output
    assert "12 products, 888 units, 4 low stock, 1 out of stock" in result.output
    result = run(app, "--yes")
    assert "Added 0 product(s); skipped 12" in result.output


def test_cli_refuses_production_without_explicit_confirmation(app, empty_inventory):
    app.config["APP_ENV"] = "production"
    result = run(app, "--yes")
    assert result.exit_code != 0 and "PRODUCTION" in result.output
    result = run(app, "--allow-production", answer="yes please\n")
    assert result.exit_code != 0 and "Cancelled" in result.output
    assert counts(app)[0] == 0
    result = run(app, "--allow-production", answer="SEED DEMO DATA\n")
    assert "Added 12 product(s)" in result.output


def test_cli_detects_the_production_database_even_in_development(app, empty_inventory, monkeypatch):
    monkeypatch.setenv("DATABASE_URL_PROD", app.config["SQLALCHEMY_DATABASE_URI"])
    result = run(app, "--yes")
    assert result.exit_code != 0 and "PRODUCTION" in result.output
    assert counts(app)[0] == 0
