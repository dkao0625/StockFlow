"""
Database models for StockFlow.

Each class below becomes one PostgreSQL table. Flask-SQLAlchemy turns
Python attribute access into parameterized SQL, so we never build SQL
strings by hand (which protects against SQL injection).
"""

from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# Allowed values, kept in one place so routes, templates, and
# database constraints all agree.
ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
# The manager sent it back: the employee fixes it and resubmits (-> Pending).
STATUS_NEEDS_CORRECTION = "Needs Correction"
# Restock requests only: after approval, stock arrives in one or more deliveries.
STATUS_PARTIALLY_RECEIVED = "Partially Received"
STATUS_RECEIVED = "Received"

REQUEST_REASONS = ["Damaged", "Missing"]  # the employee "Report a problem" form

# Inventory requests come in two types, both reviewed in the same manager queue.
REQUEST_ADJUSTMENT = "adjustment"  # change stock up or down
REQUEST_ARCHIVE = "archive"        # remove (archive) a product
REQUEST_RESTOCK = "restock"        # ask to order more; approval does NOT change stock
REQUEST_COUNT = "count"            # "I counted N": stock is SET to N when approved
REQUEST_ISSUE = "issue"            # any other problem; approval never changes stock
DIRECTION_INCREASE = "increase"
DIRECTION_DECREASE = "decrease"

# Which reasons fit which direction of stock change.
DECREASE_REASONS = ["Damaged", "Missing", "Count correction"]
INCREASE_REASONS = ["Received", "Found", "Count correction"]
ARCHIVE_REASON = "Archive"
RESTOCK_REASON = "Restock"
COUNT_REASON = "Count correction"
ISSUE_REASON = "Other"

# Request types as managers see them on the Approvals page. The key is used
# in the URL filter (?type=damaged); the value is the label shown.
REQUEST_TYPE_LABELS = {
    "increase": "Stock increase",
    "decrease": "Stock decrease",
    "damaged": "Damaged inventory",
    "missing": "Missing inventory",
    "archive": "Product deletion",
    "restock": "Restock request",
    "count": "Stock count correction",
    "issue": "Other inventory issue",
}

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

    # A deactivated account can't log in, and any session it already has is
    # rejected on its next request (see auth.load_logged_in_user).
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    # When the account was created (empty for accounts made before this
    # column existed whose creation date couldn't be worked out).
    created_at = db.Column(db.DateTime(timezone=True), nullable=True, default=utc_now)

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
        db.CheckConstraint(
            "target_stock_level IS NULL OR target_stock_level >= 0",
            name="ck_products_target_nonnegative",
        ),
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
    # Optional: how many units the manager wants on hand after restocking.
    # Used to suggest a reorder quantity on the Stock Alerts page.
    target_stock_level = db.Column(db.Integer, nullable=True)

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

    # --- Archiving (soft delete) ---
    # Products are never really deleted, because their requests and stock
    # history must stay. An archived product is just hidden from the active
    # inventory list.
    is_archived = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)
    archived_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    archived_by = db.relationship("User")

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

    @property
    def suggested_reorder_quantity(self):
        """Units needed to reach the target stock level, or None if no target is set."""
        if self.target_stock_level is None:
            return None
        return max(self.target_stock_level - self.quantity, 0)


class InventoryRequest(db.Model):
    __tablename__ = "inventory_requests"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('Pending', 'Approved', 'Rejected', 'Partially Received', 'Received', "
            "'Needs Correction')",
            name="ck_requests_status",
        ),
        db.CheckConstraint(
            "reason IN ('Damaged', 'Missing', 'Received', 'Found', 'Count correction', 'Archive', "
            "'Restock', 'Other')",
            name="ck_requests_reason",
        ),
        db.CheckConstraint(
            "request_type IN ('adjustment', 'archive', 'restock', 'count', 'issue')",
            name="ck_requests_type",
        ),
        # A stock count must say what was counted (0 is a valid count).
        db.CheckConstraint(
            "request_type <> 'count' OR (counted_quantity IS NOT NULL AND counted_quantity >= 0)",
            name="ck_requests_count_fields",
        ),
        # A restock request needs a positive quantity, and can never be
        # received more times over than the quantity that was approved.
        db.CheckConstraint(
            "request_type <> 'restock' OR (quantity > 0 AND received_quantity <= quantity)",
            name="ck_requests_restock_fields",
        ),
        db.CheckConstraint("received_quantity >= 0", name="ck_requests_received_nonnegative"),
        db.CheckConstraint("direction IN ('increase', 'decrease')", name="ck_requests_direction"),
        # A stock adjustment always needs a positive quantity and a direction.
        # (An archive request has neither.)
        db.CheckConstraint(
            "request_type <> 'adjustment' OR (quantity > 0 AND direction IS NOT NULL)",
            name="ck_requests_adjustment_fields",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    request_type = db.Column(
        db.String(20), nullable=False, default=REQUEST_ADJUSTMENT, server_default=REQUEST_ADJUSTMENT
    )
    direction = db.Column(db.String(10), nullable=True)  # "increase"/"decrease" (adjustments only)
    quantity = db.Column(db.Integer, nullable=True)      # empty for archive requests
    reason = db.Column(db.String(20), nullable=False)    # e.g. "Damaged", "Received", "Archive"
    notes = db.Column(db.String(500), nullable=False)    # employee's explanation (may be empty)
    additional_notes = db.Column(db.String(500), nullable=True)  # optional extra details
    # Stock count corrections only: the quantity the employee physically
    # counted. The difference is worked out from the CURRENT stock when a
    # manager approves (never from an old or browser-sent number).
    counted_quantity = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    manager_note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    # Set automatically every time the request row changes (decision,
    # correction request, resubmission, delivery received, ...).
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=db.text("CURRENT_TIMESTAMP"),
    )
    # Restock requests: how many of the approved units have arrived so far.
    received_quantity = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    product = db.relationship("Product")
    employee = db.relationship("User", foreign_keys=[employee_id])
    manager = db.relationship("User", foreign_keys=[manager_id])

    @property
    def is_archive(self):
        return self.request_type == REQUEST_ARCHIVE

    @property
    def is_restock(self):
        return self.request_type == REQUEST_RESTOCK

    @property
    def is_count(self):
        return self.request_type == REQUEST_COUNT

    @property
    def is_issue(self):
        return self.request_type == REQUEST_ISSUE

    @property
    def has_fixed_change(self):
        """True when the stock change is known when submitted (+5 / -3 / restock 20)."""
        return self.request_type in (REQUEST_ADJUSTMENT, REQUEST_RESTOCK)

    @property
    def editable_quantity(self):
        """The number an employee can change when resubmitting (None if there is none)."""
        if self.is_count:
            return self.counted_quantity
        if self.has_fixed_change:
            return self.quantity
        return None

    @property
    def needs_correction(self):
        return self.status == STATUS_NEEDS_CORRECTION

    @property
    def original_quantity(self):
        """The quantity first submitted (before any resubmission changed it)."""
        for event in self.events:
            if event.event == EVENT_RESUBMITTED:
                return event.quantity_before
        return self.editable_quantity

    @property
    def latest_correction(self):
        """The most recent "please correct this" event, or None."""
        corrections = [e for e in self.events if e.event == EVENT_CORRECTION_REQUESTED]
        return corrections[-1] if corrections else None

    @property
    def changes_stock_on_approval(self):
        """Adjustments and counts change stock when approved (restock waits for delivery)."""
        return self.request_type in (REQUEST_ADJUSTMENT, REQUEST_COUNT)

    @property
    def remaining_quantity(self):
        """Restock units approved but not yet received."""
        if not self.is_restock:
            return 0
        return self.quantity - self.received_quantity

    @property
    def can_receive(self):
        """True while an approved restock still has units to arrive."""
        return (
            self.is_restock
            and self.status in (STATUS_APPROVED, STATUS_PARTIALLY_RECEIVED)
            and self.remaining_quantity > 0
        )

    @property
    def type_key(self):
        """Which REQUEST_TYPE_LABELS entry this request belongs to."""
        if self.is_archive:
            return "archive"
        if self.is_restock:
            return "restock"
        if self.is_count:
            return "count"
        if self.is_issue:
            return "issue"
        if self.reason == "Damaged":
            return "damaged"
        if self.reason == "Missing":
            return "missing"
        return "increase" if self.direction == DIRECTION_INCREASE else "decrease"

    @property
    def type_label(self):
        return REQUEST_TYPE_LABELS[self.type_key]

    @property
    def signed_quantity(self):
        """+5 for an increase, -5 for a decrease, None when not known in advance."""
        if not self.has_fixed_change:
            return None
        return self.quantity if self.direction == DIRECTION_INCREASE else -self.quantity

    @property
    def summary(self):
        """Short text for tables, e.g. "Damaged -3", "Received +10", "Archive product"."""
        if self.is_archive:
            return "Archive product"
        if self.is_restock:
            return f"Restock {self.quantity}"
        if self.is_count:
            return f"Counted {self.counted_quantity}"
        if self.is_issue:
            return "Other issue"
        return f"{self.reason} {self.signed_quantity:+d}"


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


# Request history events (the submission itself is the request row).
EVENT_CORRECTION_REQUESTED = "correction_requested"
EVENT_RESUBMITTED = "resubmitted"
EVENT_APPROVED = "approved"
EVENT_REJECTED = "rejected"
EVENT_RECEIVED = "received"
EVENT_CLOSED = "closed"
EVENT_LABELS = {
    EVENT_CORRECTION_REQUESTED: "Correction requested",
    EVENT_RESUBMITTED: "Resubmitted",
    EVENT_APPROVED: "Approved",
    EVENT_REJECTED: "Rejected",
    EVENT_RECEIVED: "Stock received",
    EVENT_CLOSED: "Order closed",
}


class RequestEvent(db.Model):
    """
    One step in a request's history after it was submitted: a manager asking
    for a correction, the employee resubmitting, approval, rejection, ...
    Rows are only added, never changed, so the full history is kept.
    """

    __tablename__ = "request_events"
    __table_args__ = (
        db.CheckConstraint(
            "event IN ('correction_requested', 'resubmitted', 'approved', 'rejected', 'received', 'closed')",
            name="ck_request_events_event",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer, db.ForeignKey("inventory_requests.id"), nullable=False, index=True
    )
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(500), nullable=True)  # manager feedback / employee response
    # For resubmissions: the quantity before and after the employee's change.
    quantity_before = db.Column(db.Integer, nullable=True)
    quantity_after = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    request = db.relationship(
        "InventoryRequest", backref=db.backref("events", order_by="RequestEvent.id")
    )
    actor = db.relationship("User")

    @property
    def label(self):
        return EVENT_LABELS[self.event]


def record_event(inventory_request, actor_id, event, note=None, quantity_before=None, quantity_after=None):
    """Add a history event to the current transaction (caller commits)."""
    db.session.add(RequestEvent(
        request_id=inventory_request.id, actor_id=actor_id, event=event, note=note,
        quantity_before=quantity_before, quantity_after=quantity_after,
    ))


class StockReceipt(db.Model):
    """
    One delivery received against an approved restock request.

    A restock can arrive in several deliveries, so each delivery gets its own
    row here, linked to the request and to the stock movement it created.
    """

    __tablename__ = "stock_receipts"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_receipts_quantity_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer, db.ForeignKey("inventory_requests.id"), nullable=False, index=True
    )
    # Each receipt created exactly one stock movement.
    movement_id = db.Column(
        db.Integer, db.ForeignKey("stock_movements.id"), nullable=False, unique=True
    )
    quantity = db.Column(db.Integer, nullable=False)
    received_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    note = db.Column(db.String(255), nullable=True)

    request = db.relationship("InventoryRequest", backref=db.backref("receipts", order_by="StockReceipt.id"))
    movement = db.relationship("StockMovement")
    received_by = db.relationship("User")


# Activity log categories: key stored in the database -> label shown.
ACTIVITY_CATEGORIES = {
    "product_created": "Product created",
    "product_edited": "Product edited",
    "bulk_edit": "Bulk edit",
    "stock_increase": "Stock increase",
    "stock_decrease": "Stock decrease",
    "damage_report": "Damage / loss report",
    "request_submitted": "Request submitted",
    "request_approved": "Request approved",
    "request_rejected": "Request rejected",
    "correction_requested": "Correction requested",
    "request_resubmitted": "Request resubmitted",
    "product_archived": "Product archived",
    "stock_received": "Stock received",
    "account": "Account change",
    "login": "Log in / out",
}


class ActivityLog(db.Model):
    """
    Audit trail: who did what, and when.

    Rows are only ever ADDED. The app has no page or route that edits or
    deletes them, and protect_activity_log() below refuses any attempt to
    change one through SQLAlchemy.
    Never put passwords, session data, or other secrets in `action`.
    """

    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # The actor's role WHEN the action happened (kept even if it changes later).
    actor_role = db.Column(db.String(20), nullable=True)
    # One of ACTIVITY_CATEGORIES (empty only for very old rows).
    category = db.Column(db.String(30), nullable=True, index=True)
    action = db.Column(db.String(255), nullable=False)  # the description
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    # Which product the action was about (empty for logins, registrations, ...).
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True, index=True)
    # Which user account the action was about (account changes only).
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # +5 / -3 for stock changes; empty when no stock moved.
    quantity_change = db.Column(db.Integer, nullable=True)
    # Which request the action was about (submitting, approving, receiving, ...).
    request_id = db.Column(db.Integer, db.ForeignKey("inventory_requests.id"), nullable=True, index=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])
    product = db.relationship("Product")
    request = db.relationship("InventoryRequest")

    @property
    def category_label(self):
        return ACTIVITY_CATEGORIES.get(self.category, "Other")


def log_activity(actor_id, action, product_id=None, category=None,
                 quantity_change=None, target_user_id=None, request_id=None):
    """Add an activity log row to the current transaction (caller commits)."""
    if len(action) > 255:
        action = action[:252] + "..."  # fit the column
    actor = db.session.get(User, actor_id) if actor_id else None
    db.session.add(ActivityLog(
        actor_id=actor_id,
        actor_role=actor.role if actor else None,
        category=category,
        action=action,
        product_id=product_id,
        target_user_id=target_user_id,
        quantity_change=quantity_change,
        request_id=request_id,
    ))


@event.listens_for(Session, "before_flush")
def protect_activity_log(session, flush_context, instances):
    """Refuse to save changes to, or deletions of, existing activity log rows."""
    for obj in session.deleted:
        if isinstance(obj, ActivityLog):
            raise PermissionError("Activity log entries can't be deleted.")
    for obj in session.dirty:
        if isinstance(obj, ActivityLog) and session.is_modified(obj):
            raise PermissionError("Activity log entries can't be changed.")
