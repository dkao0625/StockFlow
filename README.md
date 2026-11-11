# StockFlow
This is the OMIS 107 Final Project.
Created by Stephanie Chen and Dylan Kao

**Live prototype:** _TODO: add Cloud Run URL here_

StockFlow is a small inventory management web app for small businesses and warehouse teams. Employees can see stock levels and report damaged or missing items. Managers review those reports, and stock is updated automatically when a manager approves one.

## The problem it solves

In many small warehouses, damaged or missing stock gets reported verbally or on paper, and the inventory count quietly drifts away from reality. StockFlow gives every report a record and an owner, sends it through a manager's approval, and adjusts inventory only when that approval happens. Every adjustment is logged.

## Roles

| Employee | Manager |
|---|---|
| View products and available quantities | View all products |
| Report damaged or missing products (quantity + explanation) | View all employee requests (employee, product, quantity, reason) |
| See the status of **their own** requests | Approve or reject requests, with a note |
| | Approving automatically subtracts stock; rejecting changes nothing |
| | View request history |

## Main workflow

1. An employee logs in and opens **My Dashboard**.
2. They choose a product, pick **Damaged** or **Missing**, and enter a quantity and an explanation.
3. The request is saved with status **Pending**.
4. A manager logs in and opens **Requests**.
5. The manager **approves** or **rejects** the request. Rejecting requires a note.
6. On approval, the server locks the request and product rows, checks there is enough stock, subtracts the quantity, and records a stock movement, all in one database transaction.
7. The employee sees the updated status and the manager's note.

## Technologies

- Python 3.12+ and **Flask**
- **Flask-SQLAlchemy** (ORM), **PostgreSQL on Neon**
- **Jinja2** templates, plain **HTML/CSS**
- **Flask-WTF** for CSRF protection, **Werkzeug** for password hashing
- **Gunicorn** + **Docker** on **Google Cloud Run**
- **pytest** for tests

## Project structure

```text
app.py              App factory, configuration, CLI commands (init-db, create-manager, seed-demo)
models.py           Database tables: User, Product, InventoryRequest, StockMovement, ActivityLog
auth.py             Register / login / logout, and the login_required / manager_required decorators
inventory.py        Inventory, employee, and manager pages; approve/reject transaction logic
templates/          base.html, login, register, inventory, employee, manager, error, _macros (shared badges/cards)
static/style.css    All styling
tests/              pytest tests
requirements.txt    Production dependencies (requirements-dev.txt adds pytest)
Dockerfile, .dockerignore   Container for Cloud Run
.env.example        Template for your local .env (placeholders only)
```

Differences from the suggested structure: `error.html` (friendly 403/404/CSRF pages), `_macros.html` (shared status badges so colors stay consistent), and `tests/` plus `requirements-dev.txt`.

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

Edit `.env` and set `SECRET_KEY` and `DATABASE_URL` (the Neon string). Keep `APP_ENV=development` locally. `.env` is in `.gitignore`, so **never commit it**.

### 4. Create tables, the first manager, and sample data

```bash
flask --app app init-db          # creates the tables in Neon (safe to re-run)
flask --app app create-manager   # prompts for username + password (hidden)
flask --app app seed-demo        # adds 6 sample products; skips any that already exist
```

There is no way to sign up as a manager through the website. `create-manager` only works for someone who has the database credentials, so the manager account is protected. `seed-demo` refuses to run unless `APP_ENV=development`.

### 5. Run

```bash
flask --app app run --debug
```

Open <http://127.0.0.1:5001>, register an employee account, and try the workflow.

## Running tests

```bash
pytest
```

The tests use a temporary SQLite file, so they don't need Neon. They cover registration (which always creates an employee), login, the login requirement on every page, employees being blocked from manager routes (HTTP 403), employees not seeing each other's requests, logout invalidating a copied session cookie, CSRF rejection, invalid quantities, approval subtracting stock, double approval, rejection leaving stock alone, and the insufficient-stock check.

**Row locking** (`SELECT … FOR UPDATE`) only takes effect on PostgreSQL. `tests/test_concurrency.py` fires two simultaneous approvals and is skipped unless you point it at a **throwaway** Postgres database. In Neon, create a separate branch (Branches → New branch, e.g. `test`) and use its connection string:

```bash
TEST_DATABASE_URL="postgresql://...test-branch...?sslmode=require" pytest
```

⚠️ Tests delete every table in `TEST_DATABASE_URL`. Never use your main database for this. The tests refuse to run if it matches `DATABASE_URL`.

## Deploying to Google Cloud Run

You need a Google Cloud project with billing enabled. Cloud Run has a free tier, but turning on billing is still required. Run these commands from the project folder:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

gcloud run deploy stockflow \
  --source . \
  --region us-east5 \
  --allow-unauthenticated \
  --set-env-vars "SECRET_KEY=YOUR_LONG_RANDOM_KEY,DATABASE_URL=YOUR_NEON_CONNECTION_STRING"
```

`--source .` makes Cloud Build build the `Dockerfile` for you, so you don't need Docker installed. When the command finishes it prints the public URL. Put it at the top of this README.

Notes:
- If your `DATABASE_URL` contains commas, use `--set-env-vars "^@^SECRET_KEY=...@DATABASE_URL=..."` (this changes the separator to `@`). A safer option is Secret Manager (`--set-secrets`).
- Run `init-db`, `create-manager`, and (for the demo) `seed-demo` **from your laptop** with `.env` pointing at the same Neon database. Cloud Run's filesystem is temporary, so all data lives in Neon.
- The container sets `APP_ENV=production`, which makes session cookies HTTPS-only.

## Security measures

- Passwords are hashed with Werkzeug and never stored in plain text.
- Sessions use Flask's signed session cookie. The cookie is HttpOnly and SameSite=Lax, plus Secure in production, and sessions expire after 8 hours.
- Logout increments the user's `session_version`, so any copy of the old cookie stops working.
- CSRF tokens are required on every form that changes data (Flask-WTF).
- Permissions are checked in Flask routes (`login_required`, `employee_required`, `manager_required`), not just by hiding buttons.
- All queries go through SQLAlchemy, so they are parameterized.
- Quantities, reasons, notes, and usernames are validated on the server.
- The database has CHECK constraints (`quantity >= 0` on products, `quantity > 0` on requests, valid status/role values), and a request can create at most one stock movement (a UNIQUE constraint).
- Secrets come only from environment variables. The app refuses to start without `SECRET_KEY` and `DATABASE_URL`.

## Checkpoint status (HW6)

**Completed**
- Employee registration, login, and logout. The first manager is created with the CLI command.
- Inventory dashboard with stock status badges and summary cards
- Employee damage/missing reports and "My requests" status list
- Manager review: approve/reject with notes, pending queue, request history
- Transactional approval with row locking, stock check, stock movements, and an activity log
- Automated tests and a Cloud Run–ready Dockerfile

**Planned for the final project**
- Managers can add and edit products and restock (positive stock movements)
- Search, filter, and pagination on inventory and request tables
- Pages for viewing the stock movement and activity log history
- Database migrations (Flask-Migrate) instead of `create_all`
- Password change and manager-managed user accounts

**Current limitations**
- Products can only be added through `seed-demo` or directly in the database (there's no UI yet).
- Schema changes require dropping or recreating tables, because there are no migrations yet.
- No rate limiting on login attempts.
- Request history shows only the 50 most recent reviewed requests.
- Times are shown in UTC.

## Team contributions

_TODO: describe each member's contributions._

- Stephanie Chen: _TBD_
- Dylan Kao: _TBD_
