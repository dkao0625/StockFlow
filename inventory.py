"""
Inventory pages and the employee -> manager request workflow.

Workflow:
  1. An employee submits a request -> status "Pending". A request is either
       - a stock adjustment (Damaged/Missing report, or a bulk "Adjust stock"
         increase/decrease), or
       - an archive request (bulk "Delete" from the inventory page).
  2. Manager approves -> the change is applied (stock moves, or the product
     is archived) and recorded.
     Manager rejects  -> nothing changes.
  3. Employee sees the updated status on their dashboard.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import employee_required, login_required, manager_required
from models import (
    DIRECTION_DECREASE,
    PRODUCT_CATEGORIES,
    REQUEST_ADJUSTMENT,
    REQUEST_REASONS,
    STOCK_IN,
    STOCK_LOW,
    STOCK_OUT,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    DIRECTION_INCREASE,
    REQUEST_ARCHIVE,
    REQUEST_COUNT,
    REQUEST_ISSUE,
    REQUEST_RESTOCK,
    REQUEST_TYPE_LABELS,
    COUNT_REASON,
    DECREASE_REASONS,
    INCREASE_REASONS,
    ISSUE_REASON,
    STATUS_PARTIALLY_RECEIVED,
    STATUS_RECEIVED,
    STATUS_NEEDS_CORRECTION,
    EVENT_APPROVED,
    EVENT_CORRECTION_REQUESTED,
    EVENT_REJECTED,
    EVENT_RESUBMITTED,
    record_event,
    ActivityLog,
    InventoryRequest,
    Product,
    StockMovement,
    User,
    db,
    log_activity,
)
from csv_export import MAX_EXPORT_ROWS, csv_response
from products import safe_next_url
from stock import MAX_STOCK_QUANTITY, StockError, archive_product, change_stock, lock_products

inventory_bp = Blueprint("inventory", __name__)

MAX_REQUEST_QUANTITY = 100_000
MAX_NOTE_LENGTH = 500
STOCK_STATUSES = [STOCK_IN, STOCK_LOW, STOCK_OUT]


def active_products():
    """Products that are not archived, by name."""
    return Product.query.filter_by(is_archived=False).order_by(Product.name).all()


def inventory_stats(products):
    """Numbers for the summary cards at the top of the dashboards."""
    return {
        "total_products": len(products),
        "total_units": sum(p.quantity for p in products),
        "low_stock": sum(1 for p in products if p.stock_status == STOCK_LOW),
        "out_of_stock": sum(1 for p in products if p.stock_status == STOCK_OUT),
    }


def parse_positive_quantity(raw_value):
    """Turn form text into a whole number from 1 to MAX_REQUEST_QUANTITY, or None."""
    try:
        quantity = int(raw_value)
    except (TypeError, ValueError):
        return None
    if quantity < 1 or quantity > MAX_REQUEST_QUANTITY:
        return None
    return quantity


# ---------------------------------------------------------------------------
# Pages any logged-in user can see
# ---------------------------------------------------------------------------

@inventory_bp.route("/")
def index():
    if g.user is None:
        return redirect(url_for("auth.login"))
    if g.user.is_manager:
        return redirect(url_for("manager.overview"))
    return redirect(url_for("inventory.employee_dashboard"))


def inventory_filters():
    """Search and filters from the URL, e.g. /inventory?q=tape&category=Other"""
    return {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", ""),
        "status": request.args.get("status", ""),
        "show": request.args.get("show", "active"),  # "active" or "archived"
    }


def filter_products(filters):
    """Products matching the Inventory page filters (used by the page and its CSV export)."""
    query = Product.query.filter_by(is_archived=filters["show"] == "archived")
    if filters["q"]:
        # ilike = case-insensitive LIKE. SQLAlchemy sends the search text as a
        # parameter, never pasted into the SQL, so this is injection-safe.
        pattern = f"%{filters['q']}%"
        query = query.filter(
            db.or_(Product.name.ilike(pattern), Product.sku.ilike(pattern), Product.location.ilike(pattern))
        )
    if filters["category"] in PRODUCT_CATEGORIES:
        query = query.filter_by(category=filters["category"])
    products = query.order_by(Product.name).all()

    # Status isn't stored (it's calculated), so it's filtered in Python.
    if filters["status"] in STOCK_STATUSES:
        products = [p for p in products if p.stock_status == filters["status"]]
    return products


@inventory_bp.route("/inventory")
@login_required
def inventory_list():
    filters = inventory_filters()
    showing_archived = filters["show"] == "archived"
    products = filter_products(filters)

    return render_template(
        "inventory.html",
        products=products,
        stats=inventory_stats(active_products()),
        filters=filters,
        showing_archived=showing_archived,
        is_filtered=bool(filters["q"] or filters["category"] or filters["status"]),
        categories=PRODUCT_CATEGORIES,
        statuses=STOCK_STATUSES,
    )


# ---------------------------------------------------------------------------
# Employee pages
# ---------------------------------------------------------------------------

# My Requests status filter (?status=...) -> request statuses it includes.
# "Approved" also covers restocks that are being / have been received.
MY_REQUEST_FILTERS = {
    "pending": [STATUS_PENDING],
    "needs_correction": [STATUS_NEEDS_CORRECTION],
    "approved": [STATUS_APPROVED, STATUS_PARTIALLY_RECEIVED, STATUS_RECEIVED],
    "rejected": [STATUS_REJECTED],
}
EMPLOYEE_RECENT_LIMIT = 5
MY_REQUESTS_PER_PAGE = 20
# Activity categories that aren't inventory work (left off the dashboard).
NON_INVENTORY_CATEGORIES = ["login", "account"]


def my_requests_query():
    """
    Only THIS employee's requests. Filtering by employee_id on the server is
    what keeps other employees' requests private.
    """
    return InventoryRequest.query.filter_by(employee_id=g.user.id)


def employee_stats():
    """The employee's own request counts, plus how many products need restocking."""
    by_status = dict(
        my_requests_query()
        .with_entities(InventoryRequest.status, db.func.count())
        .group_by(InventoryRequest.status)
        .all()
    )
    return {
        key: sum(by_status.get(status, 0) for status in statuses)
        for key, statuses in MY_REQUEST_FILTERS.items()
    } | {
        # Same rule as the Stock Alerts page: active and at/below reorder level.
        "alerts": Product.query.filter(
            Product.is_archived.is_(False), Product.quantity <= Product.reorder_level
        ).count(),
    }


@inventory_bp.route("/employee")
@employee_required
def employee_dashboard():
    recent_requests = (
        my_requests_query()
        .order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(EMPLOYEE_RECENT_LIMIT)
        .all()
    )
    recent_activity = (
        ActivityLog.query.filter(
            ActivityLog.actor_id == g.user.id,
            db.or_(ActivityLog.category.is_(None), ActivityLog.category.notin_(NON_INVENTORY_CATEGORIES)),
        )
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(EMPLOYEE_RECENT_LIMIT)
        .all()
    )
    return render_template(
        "employee.html",
        products=active_products(),
        # Pre-select a product when coming from its detail page (?product_id=5)
        selected_product_id=request.args.get("product_id", type=int),
        recent_requests=recent_requests,
        recent_activity=recent_activity,
        report_types=REPORT_TYPES,
        selected_type=request.args.get("type") if request.args.get("type") in REPORT_TYPES else "damaged",
        increase_reasons=INCREASE_REASONS,
        decrease_reasons=DECREASE_REASONS,
        my_stats=employee_stats(),
    )


@inventory_bp.route("/my-requests")
@employee_required
def my_requests():
    """Every request this employee submitted, with status/type filters and search."""
    filters = {
        "status": request.args.get("status", "all"),
        "type": request.args.get("type", ""),
        "q": request.args.get("q", "").strip(),
    }
    if filters["status"] not in MY_REQUEST_FILTERS:
        filters["status"] = "all"
    if filters["type"] not in REQUEST_TYPE_LABELS:
        filters["type"] = ""

    query = my_requests_query()
    if filters["type"]:
        query = query.filter(request_type_condition(filters["type"]))
    if filters["q"]:
        # Search product name/SKU and the explanation; "#12" or "12" finds request 12.
        pattern = f"%{filters['q']}%"
        conditions = [Product.name.ilike(pattern), Product.sku.ilike(pattern),
                      InventoryRequest.notes.ilike(pattern)]
        if filters["q"].lstrip("#").isdigit():
            conditions.append(InventoryRequest.id == int(filters["q"].lstrip("#")))
        query = query.join(Product, InventoryRequest.product_id == Product.id).filter(db.or_(*conditions))
    # Tab counts respect the type filter and search (but not the status tab).
    counts = dict(
        query.with_entities(InventoryRequest.status, db.func.count()).group_by(InventoryRequest.status).all()
    )
    tab_counts = {key: sum(counts.get(s, 0) for s in statuses) for key, statuses in MY_REQUEST_FILTERS.items()}
    tab_counts["all"] = sum(counts.values())

    if filters["status"] != "all":
        query = query.filter(InventoryRequest.status.in_(MY_REQUEST_FILTERS[filters["status"]]))
    page = query.order_by(InventoryRequest.updated_at.desc(), InventoryRequest.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=MY_REQUESTS_PER_PAGE, error_out=False
    )
    return render_template(
        "my_requests.html",
        requests=page.items,
        page=page,
        filters=filters,
        args={key: value for key, value in filters.items() if value and value != "all"},
        counts=tab_counts,
        type_labels=REQUEST_TYPE_LABELS,
    )


# The "Report a problem" form's request types: form value -> label.
REPORT_TYPES = {
    "damaged": "Damaged inventory",
    "missing": "Missing inventory",
    "count": "Stock count correction",
    "adjust": "Stock adjustment (increase / decrease)",
    "restock": "Restock request",
    "issue": "Other inventory issue",
}


def parse_count(raw_value):
    """A counted quantity: a whole number from 0 to MAX_STOCK_QUANTITY, or None."""
    try:
        counted = int(raw_value)
    except (TypeError, ValueError):
        return None
    return counted if 0 <= counted <= MAX_STOCK_QUANTITY else None


def read_report_form(form):
    """
    Validate the Report a problem form for the chosen type.
    Returns (values, error). Only the fields that type uses are read; the
    rest are ignored, so hidden fields can't sneak values in.
    """
    report_type = form.get("request_type", "")
    if not report_type:
        # Older form (and tests) send only reason=Damaged/Missing.
        report_type = {"Damaged": "damaged", "Missing": "missing"}.get(form.get("reason", ""), "")
    if report_type not in REPORT_TYPES:
        return None, "Please choose a request type."

    product_id = form.get("product_id", type=int)
    product = db.session.get(Product, product_id) if product_id else None
    if product is None or product.is_archived:
        return None, "Please choose a valid product."

    values = {
        "type": report_type,
        "product": product,
        "notes": form.get("notes", "").strip(),
        "additional_notes": form.get("additional_notes", "").strip() or None,
    }

    if report_type in ("damaged", "missing", "adjust", "restock"):
        values["quantity"] = parse_positive_quantity(form.get("quantity"))
        if values["quantity"] is None:
            return None, f"Quantity must be a whole number between 1 and {MAX_REQUEST_QUANTITY:,}."

    if report_type in ("damaged", "missing"):
        values["direction"] = DIRECTION_DECREASE
        values["reason"] = "Damaged" if report_type == "damaged" else "Missing"
    elif report_type == "adjust":
        values["direction"] = form.get("direction", "")
        values["reason"] = form.get("adjust_reason", "")
        allowed = {DIRECTION_INCREASE: INCREASE_REASONS, DIRECTION_DECREASE: DECREASE_REASONS}
        if values["direction"] not in allowed:
            return None, "Choose Increase or Decrease."
        if values["reason"] not in allowed[values["direction"]]:
            return None, f"Choose a reason: {', '.join(allowed[values['direction']])}."
    elif report_type == "count":
        values["counted_quantity"] = parse_count(form.get("counted_quantity"))
        if values["counted_quantity"] is None:
            return None, f"Counted quantity must be a whole number from 0 to {MAX_STOCK_QUANTITY:,}."

    # Decreases can't ask for more than is on the shelf right now (checked
    # again with a row lock when a manager approves).
    if values.get("direction") == DIRECTION_DECREASE and values["quantity"] > product.quantity:
        return None, f"Only {product.quantity} unit(s) of {product.name} are in stock."
    if not values["notes"]:
        return None, "Please add a short explanation."
    if len(values["notes"]) > MAX_NOTE_LENGTH:
        return None, f"Explanation must be {MAX_NOTE_LENGTH} characters or fewer."
    if values["additional_notes"] and len(values["additional_notes"]) > MAX_NOTE_LENGTH:
        return None, f"Additional notes must be {MAX_NOTE_LENGTH} characters or fewer."
    return values, None


@inventory_bp.route("/requests", methods=["POST"])
@employee_required
def submit_request():
    """
    The employee "Report a problem" form. Every type creates a PENDING
    request; nothing in inventory changes until a manager approves (and,
    for restocks, until the delivery is received).
    """
    values, error = read_report_form(request.form)
    if error:
        flash(error, "error")
        # Come back to the form with the same product and type selected.
        return redirect(url_for(
            "inventory.employee_dashboard",
            product_id=request.form.get("product_id") or None,
            type=request.form.get("request_type") or None,
        ) + "#report")

    product = values["product"]
    if values["type"] == "restock":
        from restock import create_restock  # (imported here: restock.py imports this file)
        new_request = create_restock(product, values["quantity"], values["notes"], values["additional_notes"])
        db.session.commit()
        flash(f"Restock request #{new_request.id} submitted. A manager will review it.", "success")
        return redirect(url_for("inventory.employee_dashboard"))

    new_request = InventoryRequest(
        product_id=product.id,
        employee_id=g.user.id,
        notes=values["notes"],
        additional_notes=values["additional_notes"],
        status=STATUS_PENDING,
    )
    if values["type"] == "count":
        new_request.request_type = REQUEST_COUNT
        new_request.reason = COUNT_REASON
        new_request.counted_quantity = values["counted_quantity"]
        description = f"counted {values['counted_quantity']} of {product.sku} (recorded {product.quantity})"
    elif values["type"] == "issue":
        new_request.request_type = REQUEST_ISSUE
        new_request.reason = ISSUE_REASON
        description = f"other issue with {product.sku}"
    else:  # damaged, missing, adjust
        new_request.request_type = REQUEST_ADJUSTMENT
        new_request.direction = values["direction"]
        new_request.quantity = values["quantity"]
        new_request.reason = values["reason"]
        description = f"{values['reason']} {new_request.signed_quantity:+d} of {product.sku}"

    db.session.add(new_request)
    db.session.flush()  # gives new_request an id for the log message
    log_activity(
        g.user.id,
        f"Submitted request #{new_request.id}: {description}",
        product.id,
        category="damage_report" if values["type"] in ("damaged", "missing") else "request_submitted",
        request_id=new_request.id,
    )
    db.session.commit()

    flash(f"{new_request.type_label} submitted as request #{new_request.id}. A manager will review it.", "success")
    return redirect(url_for("inventory.employee_dashboard"))


# ---------------------------------------------------------------------------
# Manager pages
# ---------------------------------------------------------------------------

REQUEST_STATUSES = [
    STATUS_PENDING, STATUS_NEEDS_CORRECTION, STATUS_APPROVED, STATUS_REJECTED,
    STATUS_PARTIALLY_RECEIVED, STATUS_RECEIVED,
]
APPROVALS_PER_PAGE = 25


def request_type_condition(type_key):
    """The SQL filter for one request type (see REQUEST_TYPE_LABELS in models.py)."""
    is_adjustment = InventoryRequest.request_type == REQUEST_ADJUSTMENT
    return {
        "archive": InventoryRequest.request_type == REQUEST_ARCHIVE,
        "restock": InventoryRequest.request_type == REQUEST_RESTOCK,
        "count": InventoryRequest.request_type == REQUEST_COUNT,
        "issue": InventoryRequest.request_type == REQUEST_ISSUE,
        "damaged": db.and_(is_adjustment, InventoryRequest.reason == "Damaged"),
        "missing": db.and_(is_adjustment, InventoryRequest.reason == "Missing"),
        "increase": db.and_(is_adjustment, InventoryRequest.direction == DIRECTION_INCREASE),
        "decrease": db.and_(
            is_adjustment,
            InventoryRequest.direction == DIRECTION_DECREASE,
            InventoryRequest.reason.notin_(["Damaged", "Missing"]),
        ),
    }[type_key]


def approval_filters():
    filters = {
        "status": request.args.get("status", STATUS_PENDING),  # "all" shows every status
        "type": request.args.get("type", ""),
        "employee": request.args.get("employee", type=int),
    }
    if filters["status"] not in REQUEST_STATUSES:
        filters["status"] = "all"
    return filters


def requests_matching(filters):
    """Requests matching the type and employee filters (status is applied by the caller)."""
    query = InventoryRequest.query
    if filters["type"] in REQUEST_TYPE_LABELS:
        query = query.filter(request_type_condition(filters["type"]))
    if filters["employee"]:
        query = query.filter(InventoryRequest.employee_id == filters["employee"])
    return query


# "/manager" was this page's old address; it still works so old links don't break.
@inventory_bp.route("/manager")
@inventory_bp.route("/approvals")  # registered first, so url_for() builds /approvals
@manager_required
def manager_dashboard():
    """Approvals: every employee request, with filters. Pending is shown by default."""
    filters = approval_filters()
    query = requests_matching(filters)

    # Counts for the status tabs (respecting the type/employee filters).
    status_counts = dict(
        query.with_entities(InventoryRequest.status, db.func.count())
        .group_by(InventoryRequest.status)
        .all()
    )
    status_counts["all"] = sum(status_counts.values())

    if filters["status"] != "all":
        query = query.filter(InventoryRequest.status == filters["status"])
    if filters["status"] == STATUS_PENDING:
        # The review queue: oldest first, so nothing waits forever.
        query = query.order_by(InventoryRequest.created_at, InventoryRequest.id)
    else:
        query = query.order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())

    page = query.paginate(
        page=request.args.get("page", 1, type=int), per_page=APPROVALS_PER_PAGE, error_out=False
    )
    employees = (
        User.query.filter(User.id.in_(db.select(InventoryRequest.employee_id)))
        .order_by(User.username)
        .all()
    )
    return render_template(
        "approvals.html",
        requests=page.items,
        page=page,
        filters=filters,
        status_counts=status_counts,
        statuses=REQUEST_STATUSES,
        type_labels=REQUEST_TYPE_LABELS,
        employees=employees,
        has_pending=any(r.status == STATUS_PENDING for r in page.items),
    )


@inventory_bp.route("/requests/<int:request_id>")
@login_required
def request_detail(request_id):
    """
    One request with everything about it. Managers can see any request;
    employees only their own (others get "not found", so request ids from
    other employees can't even be confirmed to exist).
    """
    inventory_request = db.get_or_404(InventoryRequest, request_id)
    if not g.user.is_manager and inventory_request.employee_id != g.user.id:
        abort(404)
    movement = StockMovement.query.filter_by(request_id=inventory_request.id).first()
    return render_template(
        "request_detail.html",
        req=inventory_request,
        movement=movement,
        receipts=inventory_request.receipts,
    )


# ---------------------------------------------------------------------------
# Approving and rejecting requests
# ---------------------------------------------------------------------------

def lock_requests(request_ids):
    """
    Load requests with row locks (SELECT ... FOR UPDATE), ordered by id.

    While this transaction holds the lock, any other transaction that tries
    to lock the same rows must WAIT until we commit or roll back. So if two
    managers click "Approve" at the same moment, the second one waits, then
    sees the status is no longer Pending, and stops.
    """
    return db.session.execute(
        db.select(InventoryRequest)
        .where(InventoryRequest.id.in_(request_ids))
        .order_by(InventoryRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()


def apply_decision(inventory_request, product, approve, manager_note):
    """
    Approve or reject ONE locked, pending request (inside the caller's
    transaction). Raises StockError if approving would break a rule.
    """
    # Nobody reviews their own request (managers don't normally create
    # requests, but this keeps the rule true even if that ever changes).
    if inventory_request.employee_id == g.user.id:
        raise StockError(f"You can't review your own request #{inventory_request.id}.")

    if approve:
        if product.is_archived:
            raise StockError(f"{product.name} is archived.")
        if inventory_request.is_archive:
            archive_product(product, g.user.id, f"approved request #{inventory_request.id}")
        elif inventory_request.is_restock:
            # Approving a restock only AUTHORIZES the order. Stock goes up
            # later, when a manager records the delivery ("Receive stock").
            pass
        elif inventory_request.is_issue:
            # A general issue never changes stock; approving marks it resolved.
            pass
        elif inventory_request.is_count:
            # Stock count correction: the difference is calculated NOW, from the
            # locked current quantity -- not from what the employee saw when
            # they submitted (other changes may have happened since).
            difference = inventory_request.counted_quantity - product.quantity
            if difference:
                change_stock(
                    product,
                    difference,
                    g.user.id,
                    note=(f"Count correction (request #{inventory_request.id}): counted "
                          f"{inventory_request.counted_quantity}, recorded {product.quantity}"),
                    request_id=inventory_request.id,
                )
        else:
            # Stock calculation: +quantity for increases, -quantity for decreases.
            change_stock(
                product,
                inventory_request.signed_quantity,
                g.user.id,
                note=f"{inventory_request.reason} (request #{inventory_request.id})",
                request_id=inventory_request.id,
            )
        inventory_request.status = STATUS_APPROVED
    else:
        # Rejecting changes ONLY the request. Product stock is untouched.
        inventory_request.status = STATUS_REJECTED

    inventory_request.manager_id = g.user.id
    inventory_request.manager_note = manager_note or None
    record_event(inventory_request, g.user.id, EVENT_APPROVED if approve else EVENT_REJECTED,
                 note=manager_note or None)
    log_activity(
        g.user.id,
        f"{inventory_request.status} request #{inventory_request.id}: {inventory_request.summary}",
        product.id,
        category="request_approved" if approve else "request_rejected",
        request_id=inventory_request.id,
    )


def back_to_review():
    """After a decision, return to the page it was made on (Overview or Requests)."""
    return safe_next_url(url_for("inventory.manager_dashboard"))


def decide_requests(request_ids, approve):
    """
    Approve or reject several requests as ONE transaction: if any of them
    can't be approved, nothing is saved at all. Returns a redirect.
    """
    manager_note = request.form.get("manager_note", "").strip()
    error = None
    if len(manager_note) > MAX_NOTE_LENGTH:
        error = f"Note must be {MAX_NOTE_LENGTH} characters or fewer."
    elif not approve and not manager_note:
        error = "Please add a note explaining why the request is rejected."
    if error:
        flash(error, "error")
        return redirect(back_to_review())

    # ---- Start of the database transaction --------------------------------
    # Everything until commit() happens as one unit: either ALL changes are
    # saved (statuses, stock, movements, logs) or NONE are (rollback).
    try:
        locked_requests = lock_requests(request_ids)
        if len(locked_requests) != len(request_ids):
            db.session.rollback()
            abort(404)
        already_done = [r.id for r in locked_requests if r.status != STATUS_PENDING]
        if already_done:
            db.session.rollback()
            flash(f"Request #{already_done[0]} was already processed. Nothing was changed.", "warning")
            return redirect(back_to_review())

        # Lock every affected product too (request rows first, then product
        # rows -- the same order everywhere, which avoids deadlocks).
        products = lock_products({r.product_id for r in locked_requests})
        for inventory_request in locked_requests:
            apply_decision(inventory_request, products[inventory_request.product_id], approve, manager_note)

        db.session.commit()  # saves everything and releases the row locks
    except StockError as problem:
        db.session.rollback()
        if len(request_ids) == 1:
            prefix = f"Cannot {'approve' if approve else 'reject'} request #{request_ids[0]}: "
        else:
            prefix = f"Nothing was {'approved' if approve else 'rejected'}. "
        hint = " Reject that request or adjust stock first." if approve else ""
        flash(f"{prefix}{problem}{hint}", "error")
        return redirect(back_to_review())
    except Exception:
        # Any unexpected error: undo all partial changes, then re-raise so
        # Flask shows an error page and the problem is logged.
        db.session.rollback()
        raise
    # ---- End of the database transaction ----------------------------------

    verb = "approved" if approve else "rejected"
    if len(request_ids) == 1:
        message = f"Request #{request_ids[0]} {verb}."
        if not approve:
            message += " Inventory was not changed."
        elif locked_requests[0].is_restock:
            message += " The restock is authorized; stock goes up when it is received."
        elif locked_requests[0].is_issue:
            message += " The issue is marked resolved; inventory was not changed."
        elif not locked_requests[0].is_archive:
            product = locked_requests[0].product
            message += f" {product.name} stock is now {product.quantity}."
    else:
        message = f"{len(request_ids)} requests {verb}."
    flash(message, "success")
    return redirect(back_to_review())


@inventory_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@manager_required
def approve_request(request_id):
    return decide_requests([request_id], approve=True)


@inventory_bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@manager_required
def reject_request(request_id):
    return decide_requests([request_id], approve=False)


@inventory_bp.route("/requests/bulk-decision", methods=["POST"])
@manager_required
def bulk_decision():
    """Approve or reject every request ticked on the manager dashboard."""
    request_ids = sorted({int(i) for i in request.form.getlist("request_ids") if i.isdigit()})
    decision = request.form.get("decision")
    if not request_ids:
        flash("Select at least one request first.", "warning")
        return redirect(back_to_review())
    if decision not in ("approve", "reject"):
        abort(400)
    return decide_requests(request_ids, approve=(decision == "approve"))


# ---------------------------------------------------------------------------
# Correction workflow
#
#   Pending --(manager: Request correction + feedback)--> Needs Correction
#   Needs Correction --(employee: fix + response, Resubmit)--> Pending
#
# Then the manager approves or rejects as usual. Every step is saved as a
# RequestEvent, so the original values and all feedback stay in the history.
# Approved and Rejected requests are final.
# ---------------------------------------------------------------------------

@inventory_bp.route("/requests/<int:request_id>/request-correction", methods=["POST"])
@manager_required
def request_correction(request_id):
    feedback = request.form.get("manager_note", "").strip()
    if not feedback:
        flash("Write what the employee needs to correct.", "error")
        return redirect(back_to_review())
    if len(feedback) > MAX_NOTE_LENGTH:
        flash(f"Note must be {MAX_NOTE_LENGTH} characters or fewer.", "error")
        return redirect(back_to_review())

    try:
        locked = lock_requests([request_id])  # same lock as approvals
        if not locked:
            db.session.rollback()
            abort(404)
        inventory_request = locked[0]
        if inventory_request.status != STATUS_PENDING:
            db.session.rollback()
            flash(f"Request #{request_id} is {inventory_request.status}, not Pending. Nothing was changed.", "warning")
            return redirect(back_to_review())
        if inventory_request.employee_id == g.user.id:
            db.session.rollback()
            flash("You can't review your own request.", "error")
            return redirect(back_to_review())

        inventory_request.status = STATUS_NEEDS_CORRECTION
        inventory_request.manager_id = g.user.id
        inventory_request.manager_note = feedback
        record_event(inventory_request, g.user.id, EVENT_CORRECTION_REQUESTED, note=feedback)
        log_activity(
            g.user.id, f"Requested a correction to request #{request_id}: {feedback}",
            inventory_request.product_id, category="correction_requested", request_id=request_id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"Request #{request_id} was sent back to {inventory_request.employee.username} for a correction.", "success")
    return redirect(back_to_review())


def validate_resubmission(inventory_request, product, form):
    """Returns (new_quantity, response, error). Checked against the CURRENT database values."""
    response = form.get("response", "").strip()
    new_quantity = inventory_request.editable_quantity
    if product.is_archived:
        return None, None, f"{product.name} has been archived, so this request can't be resubmitted."
    if inventory_request.is_count:
        new_quantity = parse_count(form.get("quantity"))
        if new_quantity is None:
            return None, None, f"Counted quantity must be a whole number from 0 to {MAX_STOCK_QUANTITY:,}."
    elif inventory_request.has_fixed_change:
        new_quantity = parse_positive_quantity(form.get("quantity"))
        if new_quantity is None:
            return None, None, f"Quantity must be a whole number between 1 and {MAX_REQUEST_QUANTITY:,}."
        if inventory_request.changes_stock_on_approval and inventory_request.direction == DIRECTION_DECREASE \
                and new_quantity > product.quantity:
            return None, None, f"Only {product.quantity} unit(s) of {product.name} are in stock."
    if not response:
        return None, None, "Explain what you corrected for the manager."
    if len(response) > MAX_NOTE_LENGTH:
        return None, None, f"Your response must be {MAX_NOTE_LENGTH} characters or fewer."
    return new_quantity, response, None


@inventory_bp.route("/requests/<int:request_id>/resubmit", methods=["POST"])
@employee_required
def resubmit_request(request_id):
    detail_page = url_for("inventory.request_detail", request_id=request_id)
    try:
        locked = lock_requests([request_id])
        # Someone else's request looks exactly like a missing one.
        if not locked or locked[0].employee_id != g.user.id:
            db.session.rollback()
            abort(404)
        inventory_request = locked[0]
        if not inventory_request.needs_correction:
            db.session.rollback()
            flash(f"Only requests marked Needs Correction can be resubmitted (this one is {inventory_request.status}).", "warning")
            return redirect(detail_page)

        product = lock_products([inventory_request.product_id])[inventory_request.product_id]
        new_quantity, response, error = validate_resubmission(inventory_request, product, request.form)
        if error:
            db.session.rollback()
            flash(error, "error")
            return redirect(detail_page)

        old_quantity = inventory_request.editable_quantity
        if inventory_request.is_count:
            inventory_request.counted_quantity = new_quantity
        elif inventory_request.has_fixed_change:
            inventory_request.quantity = new_quantity
        inventory_request.status = STATUS_PENDING
        # The manager's feedback is kept in the history; clear it from the
        # request so the queue doesn't show an old note on a fresh review.
        inventory_request.manager_id = None
        inventory_request.manager_note = None
        record_event(inventory_request, g.user.id, EVENT_RESUBMITTED, note=response,
                     quantity_before=old_quantity, quantity_after=new_quantity)
        change = f" (quantity {old_quantity} → {new_quantity})" if old_quantity != new_quantity else ""
        log_activity(
            g.user.id, f"Resubmitted request #{request_id}{change}: {response}",
            inventory_request.product_id, category="request_resubmitted", request_id=request_id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    flash(f"Request #{request_id} was resubmitted. A manager will review it again.", "success")
    return redirect(detail_page)


# ---------------------------------------------------------------------------
# CSV exports (managers only). Each one uses the same filters as its page.
# ---------------------------------------------------------------------------

@inventory_bp.route("/inventory/export.csv")
@manager_required
def export_inventory():
    products = filter_products(inventory_filters())
    header = ["SKU", "Name", "Category", "Unit", "Quantity", "Reorder level", "Target stock level",
              "Status", "Location", "Supplier", "Unit cost", "Stock value", "Archived",
              "Created (UTC)", "Last updated (UTC)"]
    rows = (
        [
            p.sku, p.name, p.category, p.unit, p.quantity, p.reorder_level, p.target_stock_level,
            "Archived" if p.is_archived else p.stock_status, p.location, p.supplier,
            p.unit_cost, (p.unit_cost * p.quantity) if p.unit_cost is not None else None,
            p.is_archived, p.created_at, p.updated_at,
        ]
        for p in products
    )
    return csv_response("stockflow-inventory", header, rows)


@inventory_bp.route("/approvals/export.csv")
@manager_required
def export_requests():
    filters = approval_filters()
    query = requests_matching(filters)
    if filters["status"] != "all":
        query = query.filter(InventoryRequest.status == filters["status"])
    requests_found = (
        query.order_by(InventoryRequest.created_at.desc(), InventoryRequest.id.desc())
        .limit(MAX_EXPORT_ROWS).all()
    )
    return requests_csv(requests_found, "stockflow-requests")


def requests_csv(requests_found, filename):
    """CSV download of inventory requests (shared with the Reports page)."""
    header = ["Request ID", "Submitted (UTC)", "Employee", "Product SKU", "Product", "Type",
              "Quantity", "Counted quantity", "Received", "Reason", "Explanation", "Additional notes",
              "Status", "Reviewed by", "Manager notes"]
    rows = (
        [
            r.id, r.created_at, r.employee.username, r.product.sku, r.product.name, r.type_label,
            r.signed_quantity, r.counted_quantity, r.received_quantity if r.is_restock else None,
            None if r.is_archive else r.reason, r.notes, r.additional_notes, r.status,
            r.manager.username if r.manager else None, r.manager_note,
        ]
        for r in requests_found
    )
    return csv_response(filename, header, rows)
