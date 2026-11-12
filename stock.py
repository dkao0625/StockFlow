"""
Shared stock and archiving logic.

Every change to a product's quantity goes through change_stock(), and every
archive goes through archive_product(). Routes call these INSIDE a database
transaction and commit (or roll back) afterwards, so a bulk action either
saves everything or nothing.
"""

from models import (
    REQUEST_ADJUSTMENT,
    STATUS_APPROVED,
    STATUS_PENDING,
    InventoryRequest,
    Product,
    StockMovement,
    db,
    log_activity,
    utc_now,
)

MAX_STOCK_QUANTITY = 1_000_000


class StockError(Exception):
    """A business rule was broken (e.g. stock would go negative). Message is user-friendly."""


def lock_products(product_ids):
    """
    Load products with row locks (SELECT ... FOR UPDATE), ordered by id.

    While locked, no other transaction can change these rows until we commit
    or roll back. Always locking in id order means two bulk actions can't
    end up waiting on each other forever (a "deadlock").
    Returns {product_id: product}.
    """
    products = db.session.execute(
        db.select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
        # Always use the freshly read (locked) values, never a cached copy.
        .execution_options(populate_existing=True)
    ).scalars().all()
    return {product.id: product for product in products}


def change_stock(product, amount, actor_id, note, request_id=None):
    """
    Add (amount > 0) or remove (amount < 0) stock, and record it.

    The product must already be locked by the caller. Raises StockError
    instead of ever letting quantity go below 0.
    """
    if product.is_archived:
        raise StockError(f"{product.name} is archived, so its stock can't change.")
    new_quantity = product.quantity + amount
    if new_quantity < 0:
        raise StockError(
            f"{product.name}: only {product.quantity} in stock, can't remove {-amount}."
        )
    if new_quantity > MAX_STOCK_QUANTITY:
        raise StockError(f"{product.name}: stock can't go above {MAX_STOCK_QUANTITY:,}.")

    # Stock calculation. The In/Low/Out of Stock status is calculated from
    # quantity whenever it's displayed, so it updates automatically.
    product.quantity = new_quantity
    db.session.add(
        StockMovement(
            product_id=product.id,
            request_id=request_id,
            quantity_change=amount,
            actor_id=actor_id,
            note=note,
        )
    )
    log_activity(
        actor_id, f"Stock {amount:+d} for {product.sku} (now {product.quantity}): {note}", product.id
    )


def pending_adjustment_count(product_id):
    """How many stock adjustment requests for this product are still Pending."""
    return InventoryRequest.query.filter_by(
        product_id=product_id, status=STATUS_PENDING, request_type=REQUEST_ADJUSTMENT
    ).count()


def archive_blocker(product):
    """Return a reason this product can't be archived right now, or None."""
    if product.is_archived:
        return "already archived"
    pending = pending_adjustment_count(product.id)
    if pending:
        # Archiving now would leave those requests pointing at a hidden
        # product, so a manager must approve or reject them first.
        return f"has {pending} pending stock request(s) that must be reviewed first"
    return None


def archive_product(product, actor_id, note):
    """
    Archive (soft delete) a locked product. Its requests and stock history
    are kept. Any other pending archive requests for it are closed as done.
    Raises StockError if archiving isn't allowed.
    """
    blocker = archive_blocker(product)
    if blocker:
        raise StockError(f"Can't archive {product.name}: it {blocker}.")

    product.is_archived = True
    product.archived_at = utc_now()
    product.archived_by_id = actor_id

    # Close other employees' pending "please delete this" requests for the
    # same product -- the product is archived now, so they're fulfilled.
    InventoryRequest.query.filter_by(
        product_id=product.id, status=STATUS_PENDING
    ).filter(InventoryRequest.request_type != REQUEST_ADJUSTMENT).update(
        {"status": STATUS_APPROVED, "manager_id": actor_id, "manager_note": "Product archived"},
        synchronize_session="fetch",
    )
    log_activity(actor_id, f"Archived product {product.sku} ({note})", product.id)
