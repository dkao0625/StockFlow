"""
Database models for StockFlow.

Each class below becomes one PostgreSQL table. Flask-SQLAlchemy turns
Python attribute access into parameterized SQL, so we never build SQL
strings by hand (which protects against SQL injection).
"""

from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# Allowed values, kept in one place so routes, templates, and
# database constraints all agree.
ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"

REQUEST_REASONS = ["Damaged", "Missing"]

PRODUCT_CATEGORIES = ["Packaging Supplies", "Clothing", "Electronics", "Office Supplies", "Other"]
UNITS_OF_MEASUREMENT = ["Pieces", "Boxes", "Rolls", "Packs", "Pairs", "Other"]

# Inventory status labels (calculated, never stored -- see Product.stock_status)
STOCK_IN = "In Stock"
STOCK_LOW = "Low Stock"
STOCK_OUT = "Out of Stock"


def utc_now():
    """Current time in UTC (stored with time zone information)."""
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint("role IN ('employee', 'manager')", name="ck_users_role"),
    )

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_EMPLOYEE)

    # Every time a user logs out, this number goes up by one. The login
    # session stores the number it was created with; if they no longer
    # match, the old session cookie is rejected. This is how logout
    # truly invalidates a session (see auth.py).
    session_version = db.Column(db.Integer, nullable=False, default=0)

    def set_password(self, plain_password):
        # Werkzeug salts and hashes the password; we never store plain text.
        self.password_hash = generate_password_hash(plain_password)

    def check_password(self, plain_password):
        return check_password_hash(self.password_hash, plain_password)

    @property
    def is_manager(self):
        return self.role == ROLE_MANAGER


class Product(db.Model):
    __tablename__ = "products"
    __table_args__ = (
        # The database itself refuses negative stock, even if a bug in
        # Python code ever tried to save one.
        db.CheckConstraint("quantity >= 0", name="ck_products_quantity_nonnegative"),
        db.CheckConstraint("reorder_level >= 0", name="ck_products_reorder_nonnegative"),
        db.CheckConstraint("unit_cost >= 0", name="ck_products_unit_cost_nonnegative"),
        db.CheckConstraint(
            "category IN ('Packaging Supplies', 'Clothing', 'Electronics', 'Office Supplies', 'Other')",
            name="ck_products_category",
        ),
        db.CheckConstraint(
            "unit IN ('Pieces', 'Boxes', 'Rolls', 'Packs', 'Pairs', 'Other')",
            name="ck_products_unit",
        ),
    )

    # --- Basic product information ---
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    # server_default = the value the DATABASE fills in for rows that existed
    # before this column was added (and for any insert that leaves it out).
    category = db.Column(db.String(40), nullable=False, default="Other", server_default="Other")
    description = db.Column(db.String(1000), nullable=True)
    unit = db.Column(db.String(20), nullable=False, default="Pieces", server_default="Pieces")

    # --- Inventory details ---
    quantity = db.Column(db.Integer, nullable=False, default=0)
    reorder_level = db.Column(db.Integer, nullable=False, default=0)
    location = db.Column(db.String(100), nullable=True)  # e.g. "Warehouse A, Aisle 3, Shelf B"

    # --- Purchasing information ---
    supplier = db.Column(db.String(120), nullable=True)
    # Numeric(10, 2) = exact decimal with 2 digits after the point (like
    # money), never a float, so 0.10 + 0.20 is exactly 0.30.
    unit_cost = db.Column(db.Numeric(10, 2), nullable=True)

    # --- Generated automatically ---
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.text("CURRENT_TIMESTAMP"),
    )
    # onupdate: SQLAlchemy sets this every time the product row is changed
    # (edits, approvals that subtract stock, stock being added).
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=db.text("CURRENT_TIMESTAMP"),
    )

    @property
    def stock_status(self):
        """
        Calculated from quantity and reorder_level every time it's read, so it
        can never be out of date (that's why there is no status column).
          quantity == 0                -> Out of Stock
          0 < quantity <= reorder_level -> Low Stock
          quantity > reorder_level      -> In Stock
        """
        if self.quantity == 0:
            return STOCK_OUT
        if self.quantity <= self.reorder_level:
            return STOCK_LOW
        return STOCK_IN


class InventoryRequest(db.Model):
    __tablename__ = "inventory_requests"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_requests_quantity_positive"),
        db.CheckConstraint(
            "status IN ('Pending', 'Approved', 'Rejected')", name="ck_requests_status"
        ),
        db.CheckConstraint("reason IN ('Damaged', 'Missing')", name="ck_requests_reason"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(20), nullable=False)  # "Damaged" or "Missing"
    notes = db.Column(db.String(500), nullable=False)  # employee's explanation
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    manager_note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    product = db.relationship("Product")
    employee = db.relationship("User", foreign_keys=[employee_id])
    manager = db.relationship("User", foreign_keys=[manager_id])


class StockMovement(db.Model):
    """A permanent record of every change to a product's quantity."""

    __tablename__ = "stock_movements"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    # unique=True: one request can create at most one stock movement,
    # a second safety net against approving the same request twice.
    request_id = db.Column(
        db.Integer, db.ForeignKey("inventory_requests.id"), nullable=True, unique=True
    )
    quantity_change = db.Column(db.Integer, nullable=False)  # negative = stock removed
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    # Who made the change and why (e.g. "Delivery from supplier").
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    note = db.Column(db.String(255), nullable=True)

    product = db.relationship("Product")
    request = db.relationship("InventoryRequest")
    actor = db.relationship("User")


class ActivityLog(db.Model):
    """Simple audit trail: who did what, and when."""

    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    actor = db.relationship("User")


def log_activity(actor_id, action):
    """Add an activity log row to the current transaction (caller commits)."""
    db.session.add(ActivityLog(actor_id=actor_id, action=action))
