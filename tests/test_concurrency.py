"""
Row-locking test: two managers approve at the same moment.

SQLite ignores SELECT ... FOR UPDATE, so this only runs when
DATABASE_URL_TEST points at a THROWAWAY PostgreSQL database
(for example a separate Neon branch). See README "Running tests".
"""

import os
import threading

import pytest

from conftest import login, product_id, submit
from models import InventoryRequest, Product, StockMovement

TEST_DB_URL = os.environ.get("DATABASE_URL_TEST") or os.environ.get("TEST_DATABASE_URL") or ""

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL.startswith("postgres"),
    reason="needs DATABASE_URL_TEST pointing at PostgreSQL",
)


def approve_simultaneously(app, request_ids):
    """Log in one manager client per request id, then fire all approvals at once."""
    clients = []
    for _ in request_ids:
        manager_client = app.test_client()
        login(manager_client, "boss")
        clients.append(manager_client)

    # Timeouts so a broken thread makes the test FAIL instead of hanging forever.
    start_together = threading.Barrier(len(request_ids), timeout=30)
    errors = []

    def approve(manager_client, request_id):
        try:
            start_together.wait()
            manager_client.post(f"/requests/{request_id}/approve")
        except Exception as problem:  # reported below, in the main thread
            errors.append(problem)

    threads = [
        threading.Thread(target=approve, args=(c, rid), daemon=True)
        for c, rid in zip(clients, request_ids)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "an approval never finished"
    assert not errors, errors


def test_same_request_approved_twice_at_once(app, client):
    login(client, "alice")
    submit(client, product_id(app, "T-1"), quantity=4)
    with app.app_context():
        request_id = InventoryRequest.query.one().id

    approve_simultaneously(app, [request_id, request_id])

    with app.app_context():
        assert Product.query.filter_by(sku="T-1").one().quantity == 6  # 10 - 4, once
        assert StockMovement.query.count() == 1


def test_two_requests_cannot_drive_stock_negative(app, client):
    pid = product_id(app, "T-2")  # 2 in stock
    login(client, "alice")
    submit(client, pid, quantity=2)
    submit(client, pid, quantity=2)
    with app.app_context():
        request_ids = [r.id for r in InventoryRequest.query.all()]

    approve_simultaneously(app, request_ids)

    with app.app_context():
        assert Product.query.filter_by(sku="T-2").one().quantity == 0
        statuses = sorted(r.status for r in InventoryRequest.query.all())
        assert statuses == ["Approved", "Pending"]
