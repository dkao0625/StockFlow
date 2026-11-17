# StockFlow
This is the OMIS 107 Final Project.
Created by Stephanie Chen and Dylan Kao

**Live prototype:** https://stockflow-22360269486.us-east5.run.app

StockFlow is a small inventory management web app for small businesses and warehouse teams. Employees can see stock levels, report damaged or missing items, and ask for restocks. Managers get an operations dashboard: they review and approve requests, watch stock alerts, record deliveries, manage employee accounts, and run reports. Stock only changes through approved, recorded transactions.

## The problem it solves

In many small warehouses, damaged or missing stock gets reported verbally or on paper, and the inventory count quietly drifts away from reality. StockFlow gives every report a record and an owner, sends it through a manager's approval, and adjusts inventory only when that approval happens. Every adjustment is logged.

## Roles

| Employee | Manager |
|---|---|
| View, search, and filter inventory | Everything an employee can do, plus: |
| Add new products (without unit cost) | Set and see unit cost |
| Edit name, SKU, category, description, unit, location, supplier (one product or in bulk) | Also edit reorder level (one product or in bulk) |
| **Request** stock adjustments (one or many products), which go to Pending | Adjust stock **directly** (Add stock, or bulk Adjust Stock), always recorded |
| **Request** deletion of products, which goes to Pending | **Archive** products directly (with confirmation) |
| Report damaged or missing products | Approve or reject any request, one at a time or in bulk |
| See the status of **their own** requests and **My Activity** | View all requests, request history, and the full **Activity** log |
| View **Stock Alerts** (read-only) and **request a restock** | Set reorder/target levels, authorize restocks, and **Receive stock** when it arrives |
| | **Overview** dashboard, **Employees** (activate/deactivate accounts), **Reports**, and **CSV exports** |

Employees never change stock quantities or archive products themselves. Every one of those actions becomes a Pending request in the manager's queue. All of these rules are checked in the Flask routes, so hand-crafted requests get HTTP 403.

## Main workflow

1. An employee logs in and opens **My Dashboard**.
2. They choose a product, pick **Damaged** or **Missing**, and enter a quantity and an explanation.
3. The request is saved with status **Pending**.
4. A manager logs in, sees it on the **Overview**, and opens **Approvals**.
5. The manager **approves** or **rejects** the request. Rejecting requires a note.
6. On approval, the server locks the request and product rows, checks there is enough stock, subtracts the quantity, and records a stock movement, all in one database transaction.
7. The employee sees the updated status and the manager's note.

The same queue handles **bulk stock adjustments** (increases and decreases), **deletion requests** from the inventory page's Edit Inventory mode, and **restock requests**. Approving a deletion request archives the product.

### Restock workflow

1. Anyone spots a product on **Stock Alerts** and clicks **Create restock request** (quantity + reason). If the product has a *target stock level*, the quantity is suggested as `target − quantity`; otherwise it's entered by hand.
2. An employee's request is **Pending**; a manager's own request is authorized immediately.
3. **Approving a restock does not change inventory.** It only authorizes the order.
4. When the delivery arrives, a manager opens **Restock orders** (from Stock Alerts) and clicks **Receive stock** with the number of units that actually came. Inventory goes up, a stock movement and a receipt are recorded, and the request becomes **Partially Received** or **Received**.
5. You can't receive more than is still expected. Extra units must be added separately with **Add stock** on the product page (with a note). If the rest will never arrive, **Close order** (requires a note) stops waiting without changing stock.

## Manager pages

| Page | What it shows |
|---|---|
| **Overview** (`/overview`, the manager home page) | Six clickable cards (products, units, pending requests, low stock, out of stock, inventory value), the 5 newest pending requests with Approve/Reject, stock alerts, and the 10 newest inventory actions. Inventory value = quantity × unit cost, and the card says how many in-stock products have no cost so the total isn't misleading. |
| **Approvals** (`/approvals`) | Every request with status tabs (All / Pending / Approved / Rejected), type and employee filters, pages of 25, single or bulk approve/reject, and a detail page per request. |
| **Stock Alerts** (`/alerts`) | Low and out-of-stock products with search, sorting, reorder level, target level, suggested reorder quantity, and units already on order. Managers can change reorder/target levels here. |
| **Restock orders** (`/restock`) | Restocks awaiting delivery, pending, or received, with **Receive stock** and **Close order**. |
| **Activity** (`/activity`) | Every logged action with user, role, action type, product or account, description, and quantity change. Search, date range, action type, user and role filters, pages of 50, and CSV export. Entries can't be edited or deleted. |
| **Employees** (`/employees`) | All accounts with role, status, join date, request count, and last activity. Profiles show requests and activity. Managers can deactivate/reactivate accounts. |
| **Reports** (`/reports`) | Inventory summary (now), stock movements in a date range (added, removed, damaged/missing, by day), and employee requests in that range (by status, employee, and type). Simple CSS bar charts. |

**CSV exports** (managers only): inventory, stock alerts, approvals, activity, stock movements, and requests. Each export uses the filters of the page it comes from.

### Inventory edit mode

On **Inventory**, click **Edit Inventory** to show checkboxes and a sticky toolbar: *N selected | Bulk Edit | Adjust Stock | Delete | Cancel*.

- **Select All** ticks only the rows currently shown by the search and filters.
- **Cancel** leaves edit mode and clears the selection.
- **Bulk Edit:** switch on the fields to change. Only those fields change, on only the selected products.
- **Adjust Stock:** one row per product (increase or decrease, quantity, reason, note).
- **Delete:** archives products; nothing is permanently deleted. Archiving is blocked while a product has pending stock requests. Archived products disappear from the active list and appear under **View archived**, and their requests and stock history are kept.

Every bulk action is saved in **one database transaction**: if any row is invalid, nothing is saved.

## Technologies

- Python 3.12+ and **Flask**
- **Flask-SQLAlchemy** (ORM), **PostgreSQL on Neon**, **Flask-Migrate** (Alembic) for schema migrations
- **Jinja2** templates, plain **HTML/CSS**
- **Flask-WTF** for CSRF protection, **Werkzeug** for password hashing
- **Gunicorn** + **Docker** on **Google Cloud Run**
- **pytest** for tests

## Project structure

```text
app.py              App factory, configuration, CLI commands (create-manager, seed-demo)
models.py           Database tables: User, Product, InventoryRequest, StockMovement, StockReceipt, ActivityLog
auth.py             Register / login / logout, and the login_required / manager_required decorators
inventory.py        Inventory, employee dashboard, Approvals, request details, approve/reject logic, CSV exports
manager.py          Manager Overview dashboard
alerts.py           Stock Alerts and reorder/target level settings
restock.py          Restock requests, Restock orders, Receive stock, Close order
activity.py         Activity log, My Activity, activity CSV export
employees.py        Employees list, profiles, activate/deactivate
reports.py          Reports page and movement/request CSV exports
csv_export.py       Safe CSV downloads (formula-injection protection, safe filenames)
products.py         Add / edit / view products and add stock; product form validation and permissions
bulk.py             Bulk edit, bulk stock adjustment, and bulk delete (archive)
stock.py            Shared row-locked stock changes and archiving rules used by every route
migrations/         Flask-Migrate (Alembic) migration scripts; versions/ holds one file per schema change
templates/          base, login, register, inventory, product, product_form, bulk_edit, bulk_adjust,
                    bulk_archive, employee, overview, approvals, request_detail, alerts, restock_form,
                    restock_orders, activity, employees, employee_profile, reports, error, _macros
static/style.css    All styling
static/app.js       Small script: edit mode, checkbox selection, confirmations, and no double-submits
tests/              pytest tests
requirements.txt    Production dependencies (requirements-dev.txt adds pytest)
Dockerfile, .dockerignore   Container for Cloud Run
.env.example        Template for your local .env (placeholders only)
```

Differences from the suggested structure: `products.py` and `bulk.py` (split out so `inventory.py` stays focused on the request workflow), `stock.py` (shared stock rules), `migrations/`, `product.html` / `product_form.html`, `error.html` (friendly 403/404/CSRF pages), `_macros.html` (shared status badges so colors stay consistent), `static/app.js`, and `tests/` plus `requirements-dev.txt`.

## Local setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

### 2. Create a Neon database

1. Sign up at <https://neon.tech> (the free plan is enough).
2. Create a project, for example `stockflow`.
3. Open **Connect**, then copy the connection string. It looks like
   `postgresql://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/neondb?sslmode=require`

### 3. Configure environment variables

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste this as SECRET_KEY
```

Edit `.env` and set `SECRET_KEY`, `DATABASE_URL_DEV` (the Neon string your laptop uses), and `DATABASE_URL_PROD` (the Neon string the live site uses; can be the same database). `SITE_NAME` sets the name shown in the app, and `ADMIN_USERNAME` is the default username for `create-manager`. Keep `APP_ENV=development` locally. `.env` is in `.gitignore`, so **never commit it**.

### 4. Create tables, the first manager, and sample data

```bash
flask --app app db upgrade       # creates or upgrades the tables in Neon (safe to re-run)
flask --app app create-manager   # prompts for username + password (hidden)
flask --app app seed-demo        # adds 6 sample products; skips any that already exist
```

There is no way to sign up as a manager through the website. `create-manager` only works for someone who has the database credentials, so the manager account is protected. `seed-demo` refuses to run unless `APP_ENV=development`.

**After pulling new code from GitHub, run `flask --app app db upgrade`.** It applies any new migrations and does nothing if the database is already up to date.

## Database migrations

StockFlow uses **Flask-Migrate** (built on Alembic). Each schema change is a numbered file in `migrations/versions/`:

| Version | What it does |
|---|---|
| `0001_baseline_schema` | The original five tables |
| `0002_add_product_details` | Adds category, description, unit, location, supplier, unit cost (exact `NUMERIC(10,2)`), and created/updated timestamps to `products`. Existing products get `Other` / `Pieces` and the migration time as their timestamps. |
| `0003_archiving_and_request_types` | Adds archiving (`is_archived`, `archived_at`, `archived_by_id`) to `products`. Adds `request_type` (adjustment/archive) and `direction` (increase/decrease) to `inventory_requests`, with more allowed reasons. Adds `product_id` to `activity_logs`. Existing requests become "decrease" adjustments. |
| `0004_target_stock_level` | Adds optional `target_stock_level` to `products` (used for the suggested reorder quantity). |
| `0005_restock_workflow` | Adds `received_quantity` to `inventory_requests`, allows the `restock` type, `Restock` reason, and `Partially Received` / `Received` statuses (wider CHECK rules), and adds the `stock_receipts` table (one row per delivery). |
| `0006_accounts_and_activity` | Adds `is_active` and `created_at` to `users`, and `actor_role`, `category`, `quantity_change`, `target_user_id` (plus indexes) to `activity_logs`. Fills the new columns for existing rows (categories from the old wording, roles from users, join dates from first activity). |

Migrations `0004`–`0006` only **add** columns, a table, and indexes, or replace CHECK rules with **wider** ones; nothing is dropped or deleted.

Useful commands:

```bash
flask --app app db current       # which version is this database on?
flask --app app db upgrade       # apply new migrations
flask --app app db upgrade --sql # print the SQL without running it (good for reviewing)
flask --app app db check         # confirm models.py and the database match
```

To change the schema later: edit `models.py`, run `flask --app app db migrate -m "describe the change"`, **read the generated file** in `migrations/versions/`, then run `flask --app app db upgrade`. Alembic doesn't detect new CHECK constraints automatically, so add those by hand (see `0002`).

> **Databases created before migrations existed** (with the old `init-db` command) must be marked once with `flask --app app db stamp 0001` before running `db upgrade`. The shared Neon database has already been stamped and upgraded to `0002`.

Migrations only ever *add* to the existing tables. The baseline migration refuses to downgrade, so it can't accidentally drop your tables.

### 5. Run

```bash
flask --app app run --debug
```

Open <http://127.0.0.1:5001>, register an employee account, and try the workflow.

## Running tests

```bash
pytest
```

With `DATABASE_URL_TEST` blank, the tests use a temporary SQLite file and need no Neon (about 40 seconds). With it set to a Neon `test` branch they run on real PostgreSQL, including the row-locking tests (much slower, because every test talks to Neon over the network). To force SQLite for a quick check: `DATABASE_URL_TEST= pytest`. They cover:

- **Accounts and access:** registration (which always creates an employee), login, the login requirement on every page, employees being blocked from manager routes (HTTP 403), employees not seeing each other's requests, logout invalidating a copied session cookie, and CSRF rejection.
- **Requests:** invalid quantities, approval subtracting stock, double approval, rejection leaving stock alone, and the insufficient-stock check.
- **Products:** product creation with every field, and missing or invalid fields (duplicate SKUs, negative numbers, bad costs, unknown dropdown values, overly long text). Employees edit permitted fields but get 403 for quantity, reorder level, unit cost, and restocking. Also unit cost being hidden from employees, and the In / Low / Out of Stock rules.
- **Bulk actions (`tests/test_bulk.py`):** Select All respecting filters, bulk-editing only the switched-on fields, unselected products staying unchanged, employee stock adjustments becoming Pending requests, manager approval updating stock, all-or-nothing bulk approval, deletion requests and manager approval archiving products, archive blocking, history staying accessible, and permission bypass attempts.
- **Overview (`test_overview.py`):** manager-only access, statistics matching the database (including inventory value and missing costs), empty database, the 5 newest pending requests, alerts order, and approving/rejecting from the Overview.
- **Approvals (`test_approvals.py`):** status/type/employee filters, pagination, request detail privacy, stock + movement + log saved together, duplicate approval, insufficient stock, archived products, self-review, employees blocked, rejection notes, and all-or-nothing bulk approval.
- **Stock Alerts (`test_alerts.py`):** which products appear, filters, sorting, suggested reorder quantity, and manager-only level changes with validation.
- **Restock (`test_restock.py`):** employee and manager restock requests, approval without an inventory change, partial and full receiving with receipts and movements, over-receiving blocked, receiving only approved restocks, closing orders, and archive blocking.
- **Activity (`test_activity.py`):** every action category recorded (with before/after values and quantity changes), no passwords logged, entries can't be changed or deleted, filters, pagination, My Activity privacy, and CSV export.
- **Employees (`test_employees.py`):** manager-only access, deactivation locking out existing sessions and logins, reactivation, no self-deactivation, the last-active-manager rule, and no passwords shown.
- **Reports and CSV (`test_reports.py`):** manager-only reports and exports, movement and request totals by date range, exports respecting filters, formula-injection protection, and no secrets in any export.

**Row locking** (`SELECT … FOR UPDATE`) only takes effect on PostgreSQL. `tests/test_concurrency.py` fires two simultaneous approvals and is skipped unless you point it at a **throwaway** Postgres database. In Neon, create a separate branch (Branches → New branch, e.g. `test`) and use its connection string:

```bash
DATABASE_URL_TEST="postgresql://...test-branch...?sslmode=require" pytest
```

You can also set `DATABASE_URL_TEST` in `.env`; leave it blank to test on SQLite.

⚠️ Tests delete every table in `DATABASE_URL_TEST`. Never use your main database for this. The tests refuse to run if it matches `DATABASE_URL_DEV` or `DATABASE_URL_PROD`.

## Deploying to Google Cloud Run

You need a Google Cloud project with billing enabled. Cloud Run has a free tier, but turning on billing is still required. Run these commands from the project folder:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

# Store the secrets in Secret Manager (paste the value, then press Ctrl-D)
gcloud secrets create stockflow-secret-key --data-file=-
gcloud secrets create stockflow-database-url --data-file=-

# Let Cloud Run read them (PROJECT_NUMBER: gcloud projects describe YOUR_PROJECT_ID)
for s in stockflow-secret-key stockflow-database-url; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done

gcloud run deploy stockflow \
  --source . \
  --region us-east5 \
  --allow-unauthenticated \
  --set-secrets "SECRET_KEY=stockflow-secret-key:latest,DATABASE_URL_PROD=stockflow-database-url:latest" \
  --set-env-vars "SITE_NAME=StockFlow"
```

To redeploy after code changes: `gcloud run deploy stockflow --source . --region us-east5` (settings carry over).

`--source .` makes Cloud Build build the `Dockerfile` for you, so you don't need Docker installed. When the command finishes it prints the public URL. Put it at the top of this README.

Notes:
- To change the production database, add a new version of the secret (`gcloud secrets versions add stockflow-database-url --data-file=-`) and redeploy.
- Run commands against the **production** database from your laptop by putting `APP_ENV=production` in front (it then uses `DATABASE_URL_PROD`), e.g. `APP_ENV=production flask --app app create-manager`. Cloud Run's filesystem is temporary, so all data lives in Neon.
- **When new migrations are included, upgrade production BEFORE deploying the new code:**
  ```bash
  APP_ENV=production flask --app app db current        # see where production is
  APP_ENV=production flask --app app db upgrade --sql  # review the SQL first
  APP_ENV=production flask --app app db upgrade        # apply it
  gcloud run deploy stockflow --source . --region us-east5
  ```
  Try every migration on the `dev` branch first. Neon can also restore a branch to an earlier point in time if something goes wrong.
- The container sets `APP_ENV=production`, which makes session cookies HTTPS-only.

## Security measures

- Passwords are hashed with Werkzeug and never stored in plain text.
- Sessions use Flask's signed session cookie. The cookie is HttpOnly and SameSite=Lax, plus Secure in production, and sessions expire after 8 hours.
- Logout increments the user's `session_version`, so any copy of the old cookie stops working.
- CSRF tokens are required on every form that changes data (Flask-WTF).
- Permissions are checked in Flask routes (`login_required`, `employee_required`, `manager_required`), not just by hiding buttons.
- All queries go through SQLAlchemy, so they are parameterized.
- Quantities, reasons, notes, usernames, and every product field (required fields, lengths, dropdown values, money format) are validated on the server.
- The database has CHECK constraints (`quantity >= 0` and `unit_cost >= 0` on products, valid category/unit/status/role values, `quantity > 0` on requests). SKUs are UNIQUE, and a request can create at most one stock movement (also UNIQUE).
- Unit cost is only shown to managers.
- Deactivated accounts can't log in, and any session they already have is rejected on its next request. Managers can't deactivate themselves or the last active manager (checked with row locks). Managers can't see or set passwords, and nobody can become a manager through the website.
- The activity log is append-only: no route edits or deletes entries, and a SQLAlchemy hook refuses to save changes to existing entries. Nothing secret (passwords, session data, keys) is written to it.
- CSV exports are manager-only, never include password hashes or secrets, and prefix any cell starting with `=`, `+`, `-`, `@`, tab, or carriage return with `'` so spreadsheets can't run it as a formula.
- Secrets come only from environment variables. The app refuses to start without `SECRET_KEY` and a database URL (`DATABASE_URL_DEV` locally, `DATABASE_URL_PROD` on Cloud Run).

## Checkpoint status (HW6)

**Completed**
- Employee registration, login, and logout. The first manager is created with the CLI command.
- Inventory dashboard with stock status badges and summary cards
- Employees and managers can add products with full details: category, unit, description, location, and supplier. Only managers can set or see unit cost. The form is split into three sections, with errors shown next to each field.
- Employees and managers can edit product details. Reorder level and unit cost are manager-only, and quantity only changes through stock movements.
- Inventory search and filters (name/SKU/location, category, stock status)
- **Edit Inventory** mode: checkboxes, Select All (visible rows only), and a sticky bulk toolbar
- Bulk edit with "update this field" switches, and per-product change history
- Bulk stock adjustment: employees request it, managers apply it directly or approve requests
- Bulk delete as archiving: employees request it; managers approve or archive directly. History is kept.
- Bulk approve/reject on the manager dashboard (one transaction)
- Manager **Activity** page
- A detail page for each product showing its details, stock, status, stock history (who, when, why), and related reports
- Managers can add stock to a product (row-locked and recorded in the stock history)
- Inventory status (In / Low / Out of Stock) is calculated from quantity and reorder level, never stored
- Database migrations with Flask-Migrate
- Employee damage/missing reports and "My requests" status list
- Manager review: approve/reject with notes, pending queue, request history
- Transactional approval with row locking, stock check, stock movements, and an activity log
- Automated tests and a Cloud Run–ready Dockerfile

## Final project: manager operations

**Added**
- Manager **Overview** dashboard (real-time cards, pending approvals, stock alerts, recent activity)
- **Approvals** page with filters, pagination, request details, and stricter approval checks
- **Stock Alerts** with target stock levels and suggested reorder quantities
- **Restock workflow**: request → authorize → receive (partial or full) → close
- Expanded, append-only **Activity** log with categories, roles, quantity changes, filters, pages, and CSV
- **Employees** page: profiles, request counts, last activity, activate/deactivate
- **Reports** with simple charts, and **CSV exports** for inventory, alerts, requests, activity, and movements

**Still planned**
- Restoring (un-archiving) products
- Pop-up (modal) editing instead of separate pages
- Password change / reset

**Current limitations**
- Archived products can't be restored from the UI yet.
- The Inventory and Stock Alerts lists aren't paginated (Approvals and Activity are). A bulk action is limited to 200 products.
- Role changes (employee ↔ manager) aren't available in the app; managers are only created with `flask create-manager`.
- Over-delivered restock units must be added separately with **Add stock**; closing a restock early marks it Received (its note says how many actually arrived).
- Activity entries written before migration `0006` have no quantity-change value (their description still includes it).
- Reports group by UTC day and load the movements in the chosen range into memory, which is fine for a small business but not for millions of rows.
- The stock-status filter is applied in Python (status is calculated, not stored), which is fine for small inventories.
- Employees can add new products with a starting quantity without manager approval. The stock history and activity log record who added each product.
- Products that existed before migration `0002` show their migration time as "Created" and default to category `Other` and unit `Pieces` until a manager edits them.
- No rate limiting on login attempts.
- Times are shown in UTC.

## Team contributions

_TODO: describe each member's contributions._

- Stephanie Chen: _TBD_
- Dylan Kao: _TBD_
