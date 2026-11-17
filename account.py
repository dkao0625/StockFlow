"""
My Profile (every logged-in user, employees and managers alike).

/profile always shows the account that is LOGGED IN (g.user, from the
session). There is no user id in the URL or the form, so nobody can open or
change someone else's profile by editing a link. Managing OTHER people's
accounts stays on the separate, manager-only Employees pages.
"""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from auth import EMAIL_PATTERN, MAX_EMAIL_LENGTH, MAX_FULL_NAME_LENGTH, login_required
from models import ActivityLog, User, db, log_activity

profile_bp = Blueprint("profile", __name__)

RECENT_ACTIVITY_LIMIT = 5


@profile_bp.route("/profile")
@login_required
def my_profile():
    recent_activity = (
        ActivityLog.query.filter(ActivityLog.actor_id == g.user.id)
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(RECENT_ACTIVITY_LIMIT)
        .all()
    )
    return render_template("profile.html", user=g.user, recent_activity=recent_activity)


def read_profile_form(form):
    """
    Validate the ONLY two fields people can change about themselves.
    Anything else in the form (role, status, username, an id, ...) is never
    read, so it can't change anything. Returns (values, errors).
    """
    values = {
        "full_name": form.get("full_name", "").strip(),
        "email": form.get("email", "").strip().lower(),
    }
    errors = {}
    if not values["full_name"]:
        errors["full_name"] = "Full name is required."
    elif len(values["full_name"]) > MAX_FULL_NAME_LENGTH:
        errors["full_name"] = f"Full name must be {MAX_FULL_NAME_LENGTH} characters or fewer."

    if not values["email"]:
        errors["email"] = "Email is required."
    elif len(values["email"]) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(values["email"]):
        errors["email"] = "Enter a valid email address."
    elif User.query.filter(User.email == values["email"], User.id != g.user.id).first():
        errors["email"] = "Another account already uses that email."
    return values, errors


@profile_bp.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    """Change your OWN full name and email (always g.user, never an id from the browser)."""
    if request.method == "GET":
        values = {"full_name": g.user.full_name or "", "email": g.user.email or ""}
        return render_template("profile_edit.html", values=values, errors={})

    values, errors = read_profile_form(request.form)
    if errors:
        # Show the form again with what they typed, so nothing is lost.
        return render_template("profile_edit.html", values=values, errors=errors), 400

    changes = []
    if values["full_name"] != (g.user.full_name or ""):
        changes.append("name updated")
    if values["email"] != (g.user.email or ""):
        changes.append("email updated")
    if not changes:
        flash("Nothing changed.", "warning")
        return redirect(url_for("profile.my_profile"))

    g.user.full_name = values["full_name"]
    g.user.email = values["email"]
    try:
        # Only says WHAT changed -- personal details aren't copied into the log.
        log_activity(g.user.id, f"Updated own profile: {', '.join(changes)}",
                     category="account", target_user_id=g.user.id)
        db.session.commit()  # the UNIQUE email constraint is checked here too
    except IntegrityError:
        db.session.rollback()
        errors["email"] = "Another account already uses that email."
        return render_template("profile_edit.html", values=values, errors=errors), 400

    flash("Your profile was updated.", "success")
    return redirect(url_for("profile.my_profile"))
