"""`flask reset-manager-password`: a new password for a manager, from the command line only."""

from conftest import login
from models import ActivityLog, User, db

NEW_PASSWORD = "brandnew42pass"


def run(app, *args, answer="y\n"):
    return app.test_cli_runner().invoke(args=["reset-manager-password", *args], input=answer)


def manager(app):
    with app.app_context():
        return db.session.execute(db.select(User).filter_by(username="boss")).scalar_one()


def test_resets_the_password_and_logs_out_old_sessions(app, client):
    login(client, "boss")
    before = manager(app).session_version
    result = run(app, "--username", "boss", "--password", NEW_PASSWORD)
    assert result.exit_code == 0, result.output
    assert "Database:" in result.output and "Password updated" in result.output
    assert NEW_PASSWORD not in result.output            # never echoed

    boss = manager(app)
    assert boss.check_password(NEW_PASSWORD)
    assert boss.session_version == before + 1
    assert not boss.must_change_password
    assert client.get("/inventory").status_code == 302  # old session ended

    login(client, "boss", NEW_PASSWORD)
    assert client.get("/inventory").status_code == 200
    with app.app_context():
        entry = ActivityLog.query.filter_by(target_user_id=boss.id).order_by(ActivityLog.id.desc()).first()
        assert "Password reset" in entry.action and NEW_PASSWORD not in entry.action


def test_temporary_flag_forces_a_change_at_next_login(app):
    run(app, "--username", "boss", "--password", NEW_PASSWORD, "--temporary")
    assert manager(app).must_change_password


def test_answering_no_changes_nothing(app):
    result = run(app, "--username", "boss", "--password", NEW_PASSWORD, answer="n\n")
    assert result.exit_code != 0 and "Aborted" in result.output
    assert not manager(app).check_password(NEW_PASSWORD)


def test_refuses_employees_unknown_users_and_weak_passwords(app):
    result = run(app, "--username", "alice", "--password", NEW_PASSWORD)
    assert result.exit_code != 0 and "not a manager" in result.output
    result = run(app, "--username", "nobody", "--password", NEW_PASSWORD)
    assert result.exit_code != 0 and "No user" in result.output
    for weak in ["short1", "onlyletterslong", "boss12345678"]:
        result = run(app, "--username", "boss", "--password", weak)
        assert result.exit_code != 0
    assert not manager(app).check_password(NEW_PASSWORD)
