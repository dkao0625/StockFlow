"""
StockFlow application entry point.

Run locally:      flask --app app run --debug
Production:       gunicorn "app:create_app()"   (see Dockerfile)

Setup commands (run from your own computer, never exposed on the web):
  flask --app app init-db          Create database tables
  flask --app app create-manager   Create a manager account (prompts for password)
  flask --app app seed-demo        Add sample products (development only)
"""

import os
from datetime import timedelta

import click
from dotenv import load_dotenv
from flask import Flask, render_template
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import USERNAME_PATTERN, MIN_PASSWORD_LENGTH, auth_bp
from inventory import inventory_bp
from models import ROLE_MANAGER, Product, User, db, log_activity

# Read variables from a local .env file (if present) into os.environ.
# On Cloud Run there is no .env file; variables are set on the service.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

csrf = CSRFProtect()

SAMPLE_PRODUCTS = [
    # (sku, name, quantity, reorder_level)
    ("SF-1001", "Cardboard Shipping Box (Medium)", 240, 50),
    ("SF-1002", "Packing Tape Roll", 35, 40),
    ("SF-1003", "Bubble Wrap (50 ft)", 18, 10),
    ("SF-1004", "Wireless Barcode Label Printer", 4, 5),
    ("SF-1005", "Pallet Jack", 2, 1),
    ("SF-1006", "Safety Gloves (Pair)", 0, 20),
]


def create_app(test_config=None):
    """Application factory: builds and configures the Flask app."""
    app = Flask(__name__)

    app_env = os.environ.get("APP_ENV", "production")

    app.config.update(
        APP_ENV=app_env,
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL"),
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
            "DATABASE_URL is not set. Paste your Neon connection string into .env "
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
    csrf.init_app(app)  # every POST form must include a valid csrf_token

    app.register_blueprint(auth_bp)
    app.register_blueprint(inventory_bp)

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
    @app.cli.command("init-db")
    def init_db_command():
        """Create all tables (safe to run again; existing tables are kept)."""
        db.create_all()
        click.echo("Database tables are ready.")

    @app.cli.command("create-manager")
    @click.option("--username", prompt=True, help="Manager username")
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
        log_activity(manager.id, f"Manager account '{username}' created via CLI")
        db.session.commit()
        click.echo(f"Manager '{username}' created.")

    @app.cli.command("seed-demo")
    def seed_demo_command():
        """Add sample products. Skips any SKU that already exists."""
        if app.config["APP_ENV"] != "development":
            raise click.ClickException(
                "seed-demo only runs when APP_ENV=development."
            )
        added = 0
        for sku, name, quantity, reorder_level in SAMPLE_PRODUCTS:
            if Product.query.filter_by(sku=sku).first():
                continue  # never overwrite existing data
            db.session.add(
                Product(sku=sku, name=name, quantity=quantity, reorder_level=reorder_level)
            )
            added += 1
        db.session.commit()
        click.echo(f"Added {added} sample product(s); {len(SAMPLE_PRODUCTS) - added} already existed.")


# `python3 app.py` runs the local development server.
# Port 5001 by default because macOS AirPlay Receiver already uses 5000.
if __name__ == "__main__":
    create_app().run(debug=True, port=int(os.environ.get("PORT", 5001)))
