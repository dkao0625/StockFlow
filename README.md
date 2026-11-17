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
| **Report a problem**: damaged, missing, stock count correction, stock adjustment, restock, or other issue | Approve, reject, or **request a correction** on any request, one at a time or in bulk |
| Fix and **resubmit** requests a manager sent back | View all requests, request history, and the full **Activity** log |
| See **their own** requests (**My Requests**) and **My Activity** | |
| View **Stock Alerts** (read-only) and **request a restock** | Set reorder/target levels, authorize restocks, and **Receive stock** when it arrives |
| | **Overview** dashboard, **Employees** (create, edit, reset passwords, activate/deactivate accounts), **Reports**, and **CSV exports** |

Employees never change stock quantities or archive products themselves. Every one of those actions becomes a Pending request in the manager's queue. All of these rules are checked in the Flask routes, so hand-crafted requests get HTTP 403.

## Main workflow

1. An employee logs in and opens **My Dashboard**.
2. In **Report a problem** they pick a request type and a product and fill in the fields for that type (see below).
3. The request is saved with status **Pending**. Nothing in inventory changes yet.
4. A manager logs in, sees it on the **Overview**, and opens **Approvals**.
5. The manager **approves**, **rejects** (note required), or **requests a correction** (note required).
6. On approval, the server locks the request and product rows, checks the stock rules, changes the quantity, and records a stock movement, the request history, and the activity log, all in one database transaction.
7. The employee sees the updated status and the manager's note on **My Requests**.

### Request types (employee "Report a problem" form)

| Type | Employee enters | When a manager approves |
|---|---|---|
| Damaged / Missing inventory | quantity (1 up to what's in stock) | stock goes **down** by that amount |
| Stock count correction | the **actual counted quantity** (0 or more) | stock is **set to** the count. The difference is calculated from the current, locked stock at approval time; numbers sent by the browser are never trusted. |
| Stock adjustment | Increase or Decrease, a matching reason, quantity | stock goes up or down by that amount |
| Restock request | quantity to order, reason | the order is **authorized**; stock only goes up when a manager records the delivery (see Restock workflow) |
| Other inventory issue | explanation only | marked resolved; stock never changes |

Every type needs an explanation and can have optional additional notes. The form only shows the fields the chosen type needs; the server validates each type on its own and ignores everything else.

### Correction and resubmission

1. A manager reviews a Pending request and clicks **Request correction** with a note, e.g. "Please verify the quantity." The status becomes **Needs Correction**.
2. The employee sees a banner on **My Dashboard**, the request under **My Requests → Needs correction**, and the manager's feedback at the top of the request page.
3. Only the employee who submitted it can fix it: they change the quantity (or counted quantity) if needed, write a response, and click **Resubmit**. The status goes back to **Pending**.
4. The manager reviews it again and approves, rejects, or asks for another correction.

Approved and Rejected requests are final and can't be edited. Every step (correction requested, resubmitted with the old and new quantity, approved, rejected, received, closed) is saved in the request's **History**, so the original submission is never lost.

The same queue handles **bulk stock adjustments** (increases and decreases), **deletion requests** from the inventory page's Edit Inventory mode, and **restock requests**. Approving a deletion request archives the product.

### Restock workflow

1. Anyone spots a product on **Stock Alerts** and clicks **Create restock request** (quantity + reason). If the product has a *target stock level*, the quantity is suggested as `target − quantity`; otherwise it's entered by hand.
2. An employee's request is **Pending**; a manager's own request is authorized immediately.
3. **Approving a restock does not change inventory.** It only authorizes the order.
4. When the delivery arrives, a manager opens **Restock orders** (from Stock Alerts) and clicks **Receive stock** with the number of units that actually came. Inventory goes up, a stock movement and a receipt are recorded, and the request becomes **Partially Received** or **Received**.
5. You can't receive more than is still expected. Extra units must be added separately with **Add stock** on the product page (with a note). If the rest will never arrive, **Close order** (requires a note) stops waiting without changing stock.

## My Profile (everyone)

Click your name at the bottom of the sidebar to open **My Profile** (`/profile`). It always shows the account that is logged in. There's no user id in the URL, so nobody can open or change another person's profile.

- **Header:** initials avatar, name (or username if no name is set), username, role, and status
- **Personal information:** full name, username, email, role, account created, last login. **Edit profile** changes your full name and email only (email must be valid and not used by another account). Username, role, and status are changed only by a manager.
- **Account security:** **Change password** asks for your current password, checks the new one (10+ characters, a letter and a number, not containing your username), stores it hashed, and logs you out on your other devices.
- **Recent activity:** your 5 latest actions, with a link to **My Activity**

Profile updates and password changes are written to the activity log without the personal details or passwords themselves.

## Deleting and restoring employees

Employee accounts are **never removed from the database** ("soft delete"), so their requests, stock movements, and activity keep showing who did them.

- **Delete** (employee profile or Employees list): a dialog shows the employee's details and asks for a **required reason**. The account is marked deleted and inactive, every open session ends on its next click, and login is refused. Deleted accounts disappear from the normal Employees list and appear under **Employees → Deleted** (search, deletion-date filters, View details, Restore).
- **Restore**: the account becomes active again with a **new temporary password** (shown once) that must be changed at the next login; old sessions stay logged out.
- Managers can't delete manager accounts or themselves, and employees can't delete anyone (checked on the server).
- Every deletion and restoration is written to the activity log (**Employee deleted / Employee restored**, with the manager, the account, the time, and the reason or note). Those entries can't be edited, so the full history survives later restores; the profile shows it under **Deletion history**.
- A deleted account keeps its username and email, so they can't be reused by a new account (the error message points to Restore instead).

## Employee pages

| Page | What it shows |
|---|---|
| **My Dashboard** (`/employee`) | Cards for my pending, approved, and rejected / needs-correction requests and the number of low-stock alerts (each opens the filtered list), quick actions, the Report a problem form, my 5 newest requests with manager feedback, and my 5 newest inventory actions. |
| **My Requests** (`/my-requests`) | All my requests with status tabs (All / Pending / Needs correction / Approved / Rejected), type filter, search (product, SKU, explanation, or request #), last-updated date, and 20 per page. **Fix & resubmit** on requests that need a correction. |
| **Request details** (`/requests/<id>`) | Everything about one request: product, quantities (and the original quantity if it was changed), explanation, status, manager and comments, and the full history. Employees can only open their own. |
| **Inventory**, **Stock Alerts** | Shared with managers. Employees can **Request restock** from any alert (the product is pre-selected) but can't change levels, receive stock, or approve. |
| **My Activity** (`/my-activity`) | My own inventory actions with the action type, product, description, related request and its current status, plus date and action filters. |

## Manager pages

| Page | What it shows |
|---|---|
| **Overview** (`/overview`, the manager home page) | Six clickable cards (products, units, pending requests, low stock, out of stock, inventory value), the 5 newest pending requests with Approve/Reject, stock alerts, and the 10 newest inventory actions. Inventory value = quantity × unit cost, and the card says how many in-stock products have no cost so the total isn't misleading. |
| **Approvals** (`/approvals`) | Every request with status tabs (All / Pending / Approved / Rejected), type and employee filters, pages of 25, single or bulk approve/reject, and a detail page per request. |
| **Stock Alerts** (`/alerts`) | Low and out-of-stock products with search, sorting, reorder level, target level, suggested reorder quantity, and units already on order. Managers can change reorder/target levels here. |
| **Restock orders** (`/restock`) | Restocks awaiting delivery, pending, or received, with **Receive stock** and **Close order**. |
| **Activity** (`/activity`) | Every logged action with user, role, action type, product or account, description, and quantity change. Search, date range, action type, user and role filters, pages of 50, and CSV export. Entries can't be edited or deleted. |
| **Employees** (`/employees`) | All accounts with name, username, email, role, status, created date, last login, and request count; search by name, username, or email. **+ Add employee**, **Edit**, **Reset password**, **Deactivate / Activate**, and **Delete**. The **Deleted** filter lists deleted accounts (with deletion date, who deleted them, and why) and **Restore**. Profiles show requests, activity, and deletion history. |
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
app.py              App factory, configuration, CLI commands (create-manager, seed-products)
seed_data.py        The 12 fictional demo products used by `flask seed-products`
models.py           Database tables: User, Product, InventoryRequest, RequestEvent, StockMovement, StockReceipt, ActivityLog
auth.py             Login / logout, first-login and voluntary password change, password rules, and the login_required / manager_required decorators
inventory.py        Inventory, employee dashboard, Report a problem, My Requests, Approvals, request details,
                    approve / reject / request-correction / resubmit logic, CSV exports
manager.py          Manager Overview dashboard
alerts.py           Stock Alerts and reorder/target level settings
restock.py          Restock requests, Restock orders, Receive stock, Close order
activity.py         Activity log, My Activity, activity CSV export
employees.py        Employees list and profiles; create, edit, reset password, activate/deactivate
account.py          My Profile (/profile) and Edit profile for the logged-in user
reports.py          Reports page and movement/request CSV exports
csv_export.py       Safe CSV downloads (formula-injection protection, safe filenames)
products.py         Add / edit / view products and add stock; product form validation and permissions
bulk.py             Bulk edit, bulk stock adjustment, and bulk delete (archive)
stock.py            Shared row-locked stock changes and archiving rules used by every route
migrations/         Flask-Migrate (Alembic) migration scripts; versions/ holds one file per schema change
templates/          base, login, change_password, profile, profile_edit, employee_form, employee_created, employee_edit, inventory, product, product_form, bulk_edit, bulk_adjust,
                    bulk_archive, employee, my_requests, overview, approvals, request_detail, alerts, restock_form,
                    restock_orders, activity, employees, employee_profile, reports, error, _macros
static/style.css    All styling
static/app.js       Small script: edit mode, checkbox selection, confirmations, no double-submits, and
                    showing the right fields for each request type
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
flask --app app seed-products    # adds 12 demo products; skips any SKU that already exists
```

There is no way to sign up as a manager through the website. `create-manager` only works for someone who has the database credentials, so the manager account is protected. `seed-products` first shows which database it will change and asks for confirmation (see **Demo data** below).

### Demo data (`flask seed-products`)

Adds 12 fictional products (packaging, office supplies, electronics, clothing, and other) with descriptions, units, locations, suppliers, and unit costs. On an empty database you get **12 products, 888 units on hand, 4 low-stock products, and 1 out-of-stock product**.

- **Safe to run again:** products whose SKU already exists are skipped and never changed, so nothing is duplicated and no existing quantities change.
- **Stock history:** each new product gets a "Starting stock (demo data)" stock movement and a "Product created" activity entry, the same as adding a product in the app. No users or requests are created.
- **All or nothing:** everything is saved in one transaction; if anything fails, nothing is saved.
- **Production is protected:** the command prints the database host and `APP_ENV` first. It refuses to touch the production database (`APP_ENV=production`, or a URL pointing at the same database as `DATABASE_URL_PROD`) unless you add `--allow-production` *and* type `SEED DEMO DATA`.

```bash
flask --app app seed-products          # dev database: shows it, asks "Continue? [y/N]"
flask --app app seed-products --yes    # dev database, no question
```

(`flask seed-demo` is the old name and runs the same thing, development only.)

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

| `0007_request_corrections` | Allows the `Needs Correction` status, adds `updated_at` to `inventory_requests` (existing rows get their `created_at`), and adds the `request_events` table (request history). |
| `0008_count_and_issue_requests` | Adds `counted_quantity` and `additional_notes` to `inventory_requests`, and allows the `count` and `issue` request types and the `Other` reason. |
| `0009_activity_request_link` | Adds `request_id` to `activity_logs`. Existing entries are linked only when their text names an existing request for the same product. |
| `0010_manager_created_accounts` | Adds `full_name`, `email` (unique), `must_change_password` (false for existing accounts), and `last_login_at` (filled from the latest login entry) to `users`. |
| `0011_soft_delete_users` | Adds `is_deleted` (false for existing accounts), `deleted_at`, `deleted_by_id`, and `deletion_reason` to `users`. |

Migrations `0004`–`0011` only **add** columns, tables, and indexes, or replace CHECK rules with **wider** ones; nothing is dropped or deleted.

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

Open <http://127.0.0.1:5001>, log in as the manager, create an employee on **Employees → + Add employee**, and try the workflow.

## Running tests

```bash
pytest
```

With `DATABASE_URL_TEST` blank, the tests use a temporary SQLite file and need no Neon (about 40 seconds). With it set to a Neon `test` branch they run on real PostgreSQL, including the row-locking tests (much slower, because every test talks to Neon over the network). To force SQLite for a quick check: `DATABASE_URL_TEST= pytest`. They cover:

- **Accounts and access:** public registration being gone, login, the login requirement on every page, employees being blocked from manager routes (HTTP 403), employees not seeing each other's requests, logout invalidating a copied session cookie, and CSRF rejection.
- **Requests:** invalid quantities, approval subtracting stock, double approval, rejection leaving stock alone, and the insufficient-stock check.
- **Products:** product creation with every field, and missing or invalid fields (duplicate SKUs, negative numbers, bad costs, unknown dropdown values, overly long text). Employees edit permitted fields but get 403 for quantity, reorder level, unit cost, and restocking. Also unit cost being hidden from employees, and the In / Low / Out of Stock rules.
- **Bulk actions (`tests/test_bulk.py`):** Select All respecting filters, bulk-editing only the switched-on fields, unselected products staying unchanged, employee stock adjustments becoming Pending requests, manager approval updating stock, all-or-nothing bulk approval, deletion requests and manager approval archiving products, archive blocking, history staying accessible, and permission bypass attempts.
- **Overview (`test_overview.py`):** manager-only access, statistics matching the database (including inventory value and missing costs), empty database, the 5 newest pending requests, alerts order, and approving/rejecting from the Overview.
- **Approvals (`test_approvals.py`):** status/type/employee filters, pagination, request detail privacy, stock + movement + log saved together, duplicate approval, insufficient stock, archived products, self-review, employees blocked, rejection notes, and all-or-nothing bulk approval.
- **Stock Alerts (`test_alerts.py`):** which products appear, filters, sorting, suggested reorder quantity, and manager-only level changes with validation.
- **Restock (`test_restock.py`):** employee and manager restock requests, approval without an inventory change, partial and full receiving with receipts and movements, over-receiving blocked, receiving only approved restocks, closing orders, and archive blocking.
- **Activity (`test_activity.py`):** every action category recorded (with before/after values and quantity changes), no passwords logged, entries can't be changed or deleted, filters, pagination, My Activity privacy, and CSV export.
- **Employees (`test_employees.py`):** manager-only access, deactivation locking out existing sessions and logins, reactivation, no self-deactivation, the last-active-manager rule, and no passwords shown.
- **Employee dashboard (`test_employee_dashboard.py`):** my own request counts only, card links, alert count, empty inventory, recent requests and activity privacy, My Requests filters/pages/access, the employee sidebar, and employees and managers seeing the same inventory.
- **Corrections (`test_corrections.py`):** the full report → correction → resubmit → approve flow, notes required, only Pending can be sent back, only the owner can resubmit and only from Needs Correction, validation, duplicate approval, restock and archive corrections, archive blocking, and the My Requests filters and search.
- **Request types (`test_request_types.py`):** every Report a problem type, count corrections calculated from the stock at approval time (browser values ignored), counts of 0, adjustments with matching reasons, restock through the shared workflow, issues never changing stock, and unknown types rejected.
- **Employee access (`test_employee_access.py`):** permitted inventory editing and bulk actions (recorded under the employee), 23 manager-only routes refused, Stock Alerts as an employee, and My Activity with related requests.
- **My Profile (`test_profile.py`):** both roles see only their own profile (ids in the URL are ignored), placeholders for missing details, recent activity, the sidebar link, editing your own name and email (role, status, username, and other users can't be changed), validation and duplicate emails, and changing your password from the profile (current password required, other sessions logged out, no off-site redirects).
- **Accounts and passwords (`test_accounts.py`, `test_password_management.py`):** no public sign-up, manager-created employees, validation and duplicates, first-login password change enforced on every route, password reset, and editing employees.
- **Deleting and restoring (`test_employee_deletion.py`):** soft deletion with a required reason, confirmation details, employees/managers/self/unauthenticated blocked, sessions and logins refused, deleted accounts hidden from normal lists and shown in the Deleted view (search and dates), history kept under the employee's real name, restoring with a temporary password, and the full delete/restore history kept.
- **Demo data (`test_seed.py`):** the expected totals on an empty database, stock history matching quantities, running twice adds nothing, existing products are untouched, all-or-nothing on errors, no fake users or requests, products on the Inventory page, and the production safeguards.
- **Reports and CSV (`test_reports.py`):** manager-only reports and exports, movement and request totals by date range, exports respecting filters, formula-injection protection, and no secrets in any export.

**Row locking** (`SELECT … FOR UPDATE`) only takes effect on PostgreSQL. `tests/test_concurrency.py` fires two simultaneous approvals and is skipped unless you point it at a **throwaway** Postgres database. In Neon, create a separate branch (Branches → New branch, e.g. `test`) and use its connection string:

```bash
DATABASE_URL_TEST="postgresql://...test-branch...?sslmode=require" pytest
```

You can also set `DATABASE_URL_TEST` in `.env`; leave it blank to test on SQLite.

⚠️ Tests delete every table in `DATABASE_URL_TEST`. Never use your main database for this. The tests refuse to run if it matches `DATABASE_URL_DEV` or `DATABASE_URL_PROD`.

## Manual test guide

Run locally against the **dev** branch (`flask --app app run --debug`). Use two browsers (or a normal and a private window) so an employee and a manager can be logged in at the same time. If the dev database is empty, create an employee from **Employees → + Add employee** and run `flask --app app seed-products`.

| Feature | Try this | You should see |
|---|---|---|
| Employee dashboard | Log in as an employee | Your request cards (all 0 at first), Quick actions, Recent requests, Recent activity |
| Damaged report | Report a problem → Damaged inventory, 2 units, explanation | Pending request; stock unchanged on Inventory |
| Count correction | Report a problem → Stock count correction, enter a count lower than recorded | "Currently recorded" hint; after approval, stock equals your count |
| Stock adjustment | Quick action **Request stock adjustment** → Increase, Found, 3 | Pending; after approval stock +3 |
| Restock | Stock Alerts → **Request restock** on a low product | Product pre-selected; after approval stock is unchanged until a manager uses **Receive stock** |
| Other issue | Report a problem → Other inventory issue | After approval: "marked resolved", no stock movement |
| Correction | Manager: Approvals → type a note → **Request correction** | Employee: orange banner on the dashboard, "Needs correction" tab, feedback on the request page |
| Resubmit | Employee: open the request → change the quantity, write a response → **Resubmit** | Status Pending again; History shows "Quantity changed 5 → 3" and your response |
| Approve after resubmit | Manager: approve it | Stock changes by the corrected amount; History shows Approved |
| Privacy | Employee B: open employee A's `/requests/<id>` | "Page not found" |
| Manager-only pages | Employee: visit `/overview` or `/approvals` | 403 "You don't have permission" |
| My Activity | Employee: My Activity | Your actions with the related request and its current status; no logins |
| Deactivation | Manager: Employees → profile → Deactivate | The employee's open session is logged out on their next click |
| Add employee | Manager: Employees → **+ Add employee** → generate password | The temporary password is shown once; the account shows "Temporary password" |
| First login | Log in as the new employee with the temporary password | Only the Choose your password page works until a new password is saved |
| Reset password | Manager: Employees → **Reset password** (confirm) | New temporary password shown once; the employee is logged out and must change it at next login |
| No public sign-up | Visit `/register` | 404 Page not found |
| Delete employee | Manager: employee profile → **Delete** → enter a reason → **Delete Employee** | The employee is logged out, can't log in, and appears only under Employees → Deleted with the reason |
| Restore employee | Employees → Deleted → **Restore** (confirm) | New temporary password shown once; profile shows Active and the Deletion history |
| My Profile | Click your name at the bottom of the sidebar (as an employee and as a manager) | Your own details, initials avatar, and 5 latest actions |
| Edit profile | My Profile → **Edit profile** → change name and email → Save | "Your profile was updated." and the new values; try another account's email to see the error |
| Change password | My Profile → **Change password** | Wrong current password is rejected; after saving you return to My Profile and other browsers are logged out |

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

> ⚠️ **Always deploy to the service `stockflow` in region `us-east5`.** The secrets (`SECRET_KEY`, `DATABASE_URL_PROD`) are attached to that one service. If you leave out `--region` and pick another region at the prompt, gcloud quietly creates a **second** `stockflow` service there without any secrets. It can't start, and its URL shows `upstream connect error or disconnect/reset before headers`. To avoid this, set the default region once:
>
> ```bash
> gcloud config set run/region us-east5
> ```
>
> If it happens anyway, delete the extra service (and its image repository), e.g. for `us-west2`:
>
> ```bash
> gcloud run services delete stockflow --region us-west2
> gcloud artifacts repositories delete cloud-run-source-deploy --location us-west2
> ```

**Rolling back a bad deploy.** Every deploy creates a new *revision*, and old ones are kept. To send all traffic back to the last working version (no rebuild needed, takes seconds):

```bash
gcloud run revisions list --service stockflow --region us-east5   # newest first
gcloud run services update-traffic stockflow --region us-east5 --to-revisions REVISION_NAME=100
```

Then check the errors of the bad revision before deploying again:

```bash
gcloud logging read 'resource.labels.service_name="stockflow" AND severity>=ERROR' --freshness=15m --limit 10
```

The next normal `gcloud run deploy` sends traffic to the new revision again. Rolling back only changes the code that runs; if the bad deploy came with a database migration, the database stays upgraded (all StockFlow migrations only add things, so older code keeps working with it).

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
- **No public sign-up.** Employee accounts are created only by managers (the role is always set to Employee on the server); manager accounts only with `flask create-manager`. The old `/register` URL returns 404.
- New accounts and password resets use a **temporary password** (generated with Python's `secrets` module, shown once, never logged or put in a cookie). Until the user chooses their own password, every page except Change password and Log out is blocked on the server.
- New passwords need at least 10 characters with a letter and a number, and can't contain the username. Changing a password logs out the user's other sessions; a manager reset logs out all of them.
- Deactivated accounts can't log in, and any session they already have is rejected on its next request. Managers can't deactivate themselves or the last active manager (checked with row locks). Managers can't see or set passwords, and nobody can become a manager through the website.
- The activity log is append-only: no route edits or deletes entries, and a SQLAlchemy hook refuses to save changes to existing entries. Nothing secret (passwords, session data, keys) is written to it.
- CSV exports are manager-only, never include password hashes or secrets, and prefix any cell starting with `=`, `+`, `-`, `@`, tab, or carriage return with `'` so spreadsheets can't run it as a formula.
- Secrets come only from environment variables. The app refuses to start without `SECRET_KEY` and a database URL (`DATABASE_URL_DEV` locally, `DATABASE_URL_PROD` on Cloud Run).

## Checkpoint status (HW6)

**Completed**
- Login and logout. (Public registration was replaced by manager-created accounts in the final project.) The first manager is created with the CLI command.
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

## Final project: manager-controlled accounts

- Public registration removed; managers create employee accounts (**+ Add employee**) with a generated or typed temporary password
- Employees must choose their own password at first login (enforced on the server)
- Managers can **edit** employee info, **reset passwords** (shown once, logs the employee out), and activate/deactivate accounts
- Log in with username **or** email; last login is shown on the Employees page
- Migration `0010` adds `full_name`, `email` (unique), `must_change_password`, and `last_login_at` to `users`; existing accounts keep their passwords and aren't forced to change them

## Final project: employee workspace

**Added**
- Employee **dashboard** with personal request cards, quick actions, recent requests, and recent activity
- **My Requests** page with status tabs, type filter, search, and pagination
- **Report a problem** with six request types, including stock count corrections and other issues
- Two-way **correction and resubmission** between managers and employees, with a full request history
- **My Activity** with related requests and their current status

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
- An approved "Other inventory issue" shows the status **Approved** (meaning resolved); there's no separate "Resolved" status.
- A rejected request is final; to try again the employee submits a new request.
- The Report a problem form's show/hide of fields needs JavaScript. Without it all fields are shown, and the server still accepts only the fields for the chosen type.
- The stock-status filter is applied in Python (status is calculated, not stored), which is fine for small inventories.
- Employees can add new products with a starting quantity without manager approval. The stock history and activity log record who added each product.
- Products that existed before migration `0002` show their migration time as "Created" and default to category `Other` and unit `Pieces` until a manager edits them.
- No rate limiting on login attempts.
- Times are shown in UTC.

## Team contributions

_TODO: describe each member's contributions._

- Stephanie Chen: _TBD_
- Dylan Kao: _TBD_
