"""
Shared test setup.

Tests use a throwaway SQLite file so they run anywhere without Neon
credentials. (SQLite ignores row locks, so the concurrency part of
approval is only exercised against PostgreSQL -- see README.)
"""

import os
import sys

import pytest

# Make the project root importable (app.py, models.py, ...).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from models import ROLE_MANAGER, Product, User, db  # noqa: E402

PASSWORD = "correct-horse-1"


@pytest.fixture
def app(tmp_path):
    test_db_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    # Tests DELETE all tables. Never let them touch the real app database.
    if test_db_url == os.environ.get("DATABASE_URL"):
        pytest.exit("TEST_DATABASE_URL must be a separate, throwaway database.", returncode=2)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "SQLALCHEMY_DATABASE_URI": test_db_url,
            "WTF_CSRF_ENABLED": False,  # one test below turns it back on
        }
    )
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add_all(
            [
                Product(sku="T-1", name="Widget", quantity=10, reorder_level=3),
                Product(sku="T-2", name="Gadget", quantity=2, reorder_level=5),
            ]
        )
        for username, role in [("alice", "employee"), ("bob", "employee"), ("boss", ROLE_MANAGER)]:
            user = User(username=username, role=role)
            user.set_password(PASSWORD)
            db.session.add(user)
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


def product_id(app, sku):
    with app.app_context():
        return Product.query.filter_by(sku=sku).one().id


def submit(client, pid, quantity=3, reason="Damaged", notes="Crushed box"):
    return client.post(
        "/requests",
        data={"product_id": pid, "quantity": quantity, "reason": reason, "notes": notes},
    )
