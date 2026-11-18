"""
StockFlow application entry point.

Run locally:      flask --app app run --debug
Production:       gunicorn "app:create_app()"   (see Dockerfile)

Setup commands (run from your own computer, never exposed on the web):
  flask --app app db upgrade       Create/upgrade database tables (Flask-Migrate)
  flask --app app create-manager   Create a manager account (prompts for password)
  flask --app app reset-manager-password   New password for a manager account
  flask --app app seed-products    Add 12 demo products (asks first; refuses production)
"""

import os
from datetime import timedelta

import click
from sqlalchemy.engine import make_url
from dotenv import load_dotenv
from flask import Flask, render_template
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import USERNAME_PATTERN, MIN_PASSWORD_LENGTH, auth_bp
from inventory import inventory_bp
from products import products_bp
from bulk import bulk_bp
from manager import manager_bp
from alerts import alerts_bp
from restock import restock_bp
from activity import activity_bp
from employees import employees_bp
from reports import reports_bp
from account import profile_bp
from models import ROLE_MANAGER, User, db, log_activity

# Read variables from a local .env file (if present) into os.environ.
# On Cloud Run there is no .env file; variables are set on the service.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

csrf = CSRFProtect()
# Flask-Migrate keeps the database schema in sync with models.py using
# versioned migration scripts in the migrations/ folder.
migrate = Migrate()

def create_app(test_config=None):
    """Application factory: builds and configures the Flask app."""
    app = Flask(__name__)

    app_env = os.environ.get("APP_ENV", "production")

    # Each environment has its own database: DATABASE_URL_PROD on Cloud Run,
    # DATABASE_URL_DEV on your laptop. (DATABASE_URL_TEST is used by pytest,
    # see tests/conftest.py.) Plain DATABASE_URL is still accepted as a fallback.
    db_url_var = "DATABASE_URL_PROD" if app_env == "production" else "DATABASE_URL_DEV"

    app.config.update(
        APP_ENV=app_env,
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(db_url_var) or os.environ.get("DATABASE_URL"),
        # Name shown in the page title, header, and login screen.
        SITE_NAME=os.environ.get("SITE_NAME") or "StockFlow",
        # Suggested username when running `flask create-manager`.
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", ""),
        # Neon closes idle connections; pre_ping checks a connection is
        # alive before using it, and recycle replaces old connections.
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 280},
        # Session cookie security:
        SESSION_COOKIE_HTTPONLY=True,  # JavaScript can't read the cookie
        SESSION_COOKIE_SAMESITE="Lax",  # cookie not sent on cross-site POSTs
        # Secure = cookie only sent over HTTPS. Cloud Run uses HTTPS, but
        # local development uses plain http://, so it's off in development.
        SESSION_COOKIE_SECURE=(app_env != "development"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        WTF_CSRF_TIME_LIMIT=None,  # token valid for the whole session
    )

    if test_config:
        app.config.update(test_config)

    # Refuse to start without required settings, instead of silently
    # using an insecure default key or a throwaway local database.
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY is not set. Copy .env.example to .env and fill it in.")
    if not app.config["SQLALCHEMY_DATABASE_URI"]:
        raise RuntimeError(
            f"{db_url_var} is not set. Paste your Neon connection string into .env "
            "(Neon console -> Connect)."
        )
    # Neon gives "postgresql://..." (some providers use "postgres://").
    # We tell SQLAlchemy explicitly to use the psycopg (version 3) driver.
    database_url = app.config["SQLALCHEMY_DATABASE_URI"]
    for prefix in ("postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            database_url = "postgresql+psycopg://" + database_url[len(prefix):]
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url

    # Cloud Run sits behind a proxy that terminates HTTPS. ProxyFix lets
    # Flask know the original request was https.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    db.init_app(app)
    # render_as_batch lets the same migration files also run on SQLite.
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)  # every POST form must include a valid csrf_token

    app.register_blueprint(auth_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(bulk_bp)
    app.register_blueprint(manager_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(restock_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(profile_bp)

    @app.context_processor
    def inject_site_name():
        return {"site_name": app.config["SITE_NAME"]}

    register_error_pages(app)
    register_cli_commands(app)
    return app


def register_error_pages(app):
    @app.errorhandler(403)
    def forbidden(error):
        return render_template(
            "error.html", code=403, message="You don't have permission to view this page."
        ), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        return render_template(
            "error.html",
            code=400,
            message="Your form expired or was invalid. Please go back, refresh, and try again.",
        ), 400


def register_cli_commands(app):
    @app.cli.command("create-manager")
    @click.option(
        "--username",
        prompt=True,
        default=lambda: app.config["ADMIN_USERNAME"] or None,
        help="Manager username (defaults to ADMIN_USERNAME from .env)",
    )
    @click.password_option(help="Manager password (you will be prompted)")
    def create_manager_command(username, password):
        """Create a manager account. Only people with database access can run this."""
        username = username.strip().lower()
        if not USERNAME_PATTERN.match(username):
            raise click.ClickException(
                "Username must be 3-30 characters: letters, numbers, or underscores."
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        if User.query.filter_by(username=username).first():
            raise click.ClickException(f"User '{username}' already exists.")

        manager = User(username=username, role=ROLE_MANAGER)
        manager.set_password(password)
        db.session.add(manager)
        db.session.flush()
        log_activity(manager.id, f"Manager account '{username}' created via CLI",
                     category="account", target_user_id=manager.id)
        db.session.commit()
        click.echo(f"Manager '{username}' created.")

    @app.cli.command("reset-manager-password")
    @click.option("--username", prompt=True, help="The manager whose password to reset")
    @click.password_option(help="New password (you will be prompted; typing is hidden)")
    @click.option("--temporary", is_flag=True,
                  help="Make them choose their own password at next login.")
    def reset_manager_password_command(username, password, temporary):
        """Set a new password for a MANAGER account (employees are reset in the app)."""
        from auth import password_problem

        username = username.strip().lower()
        manager = User.query.filter_by(username=username).first()
        if manager is None:
            raise click.ClickException(f"No user named '{username}'.")
        if manager.role != ROLE_MANAGER:
            raise click.ClickException(
                f"'{username}' is not a manager. Reset employee passwords in the app "
                "(Employees -> their profile -> Reset password)."
            )
        problem = password_problem(password, username)
        if problem:
            raise click.ClickException(problem)

        where, is_production = database_description()
        click.echo(f"Database: {where}")
        if is_production:
            click.echo("This is the PRODUCTION database (the live site).")
        click.confirm(f"Set a new password for manager '{username}'?", abort=True)

        manager.set_password(password)               # only the hash is stored
        manager.must_change_password = temporary
        manager.session_version += 1                 # logs them out everywhere
        log_activity(None, f"Password reset for manager '{username}' via CLI",
                     category="account", target_user_id=manager.id)
        db.session.commit()
        click.echo(f"Password updated for '{username}'. Their open sessions were logged out.")
        if not manager.is_active:
            click.echo("Note: this account is deactivated, so it still can't log in.")

    def database_description():
        """Which database the app is connected to, WITHOUT the password: (text, is_production)."""
        target = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
        prod_url = os.environ.get("DATABASE_URL_PROD")
        same_as_prod = False
        if prod_url:
            prod = make_url(prod_url)
            same_as_prod = (prod.host, prod.database) == (target.host, target.database)
        is_production = app.config["APP_ENV"] == "production" or same_as_prod
        text = f"{target.host or 'local file'} / {target.database} (APP_ENV={app.config['APP_ENV']})"
        return text, is_production

    @app.cli.command("seed-products")
    @click.option("--yes", is_flag=True, help="Don't ask for confirmation (development databases only).")
    @click.option("--allow-production", is_flag=True,
                  help="Allow seeding the PRODUCTION database (still asks you to type a confirmation).")
    def seed_products_command(yes, allow_production):
        """Add 12 fictional demo products. Skips SKUs that already exist; never changes existing stock."""
        from inventory import active_products, inventory_stats
        from seed_data import DEMO_PRODUCTS, seed_products

        target, is_production = database_description()
        click.echo(f"Database: {target}")
        if is_production:
            if not allow_production:
                raise click.ClickException(
                    "This is the PRODUCTION database. Demo data isn't added there by accident: "
                    "re-run with --allow-production if you really want to."
                )
            typed = click.prompt('This adds demo products to PRODUCTION. Type "SEED DEMO DATA" to continue')
            if typed.strip() != "SEED DEMO DATA":
                raise click.ClickException("Cancelled. Nothing was changed.")
        elif not yes:
            click.confirm(f"Add up to {len(DEMO_PRODUCTS)} demo products to this database?", abort=True)

        added, skipped = seed_products()
        click.echo(f"Added {len(added)} product(s); skipped {len(skipped)} that already existed"
                   + (f" ({', '.join(skipped)})." if skipped else "."))
        stats = inventory_stats(active_products())
        click.echo(f"Inventory now: {stats['total_products']} products, {stats['total_units']:,} units, "
                   f"{stats['low_stock']} low stock, {stats['out_of_stock']} out of stock.")

    @app.cli.command("seed-demo")
    @click.pass_context
    def seed_demo_command(ctx):
        """Old name for seed-products (development only)."""
        if app.config["APP_ENV"] != "development":
            raise click.ClickException("seed-demo only runs when APP_ENV=development. Use seed-products.")
        ctx.invoke(seed_products_command, yes=True, allow_production=False)

# `python3 app.py` runs the local development server.
# Port 5001 by default because macOS AirPlay Receiver already uses 5000.
if __name__ == "__main__":
    create_app().run(debug=True, port=int(os.environ.get("PORT", 5001)))
