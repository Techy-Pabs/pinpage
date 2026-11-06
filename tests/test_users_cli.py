import sys

import pytest
from sqlalchemy import select

from app.cli import users as users_cli
from app.models import User


@pytest.fixture(autouse=True)
def use_test_database(monkeypatch, session_factory):
    # The command normally opens the app's database; point it at the test one
    monkeypatch.setattr(users_cli, "SessionLocal", session_factory)


def run(monkeypatch, *args, answer=None):
    monkeypatch.setattr(sys, "argv", ["users", *args])
    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt: answer)
    return users_cli.main()


def is_admin(db, email):
    db.expire_all()
    return db.scalar(select(User.is_admin).where(User.email == email))


def test_promote_asks_then_makes_an_admin(monkeypatch, capsys, db, register):
    register("owner@example.com")

    assert run(monkeypatch, "promote", "Owner@Example.com", answer="y") == 0

    assert is_admin(db, "owner@example.com") is True
    out = capsys.readouterr().out
    assert "Database:" in out
    assert "is now an admin" in out


def test_answering_no_changes_nothing(monkeypatch, db, register):
    register("owner@example.com")

    assert run(monkeypatch, "promote", "owner@example.com", answer="n") == 1
    assert is_admin(db, "owner@example.com") is False


def test_unknown_email_changes_nothing_and_creates_no_account(monkeypatch, capsys, db):
    assert run(monkeypatch, "promote", "nobody@example.com", "--yes") == 1

    assert "No user with the email nobody@example.com" in capsys.readouterr().out
    assert db.scalar(select(User).where(User.email == "nobody@example.com")) is None


def test_demoting_the_last_admin_warns(monkeypatch, capsys, db, register):
    register("owner@example.com")
    run(monkeypatch, "promote", "owner@example.com", "--yes")

    assert run(monkeypatch, "demote", "owner@example.com", "--yes") == 0

    assert is_admin(db, "owner@example.com") is False
    assert "no admins left" in capsys.readouterr().out


def test_database_label_never_shows_the_password(monkeypatch):
    monkeypatch.setattr(
        users_cli.settings, "database_url", "postgresql+psycopg://someone:secret-pw@db.example.test:5432/pinpage"
    )

    label = users_cli.database_label()

    assert label == "db.example.test:5432/pinpage"
    assert "secret-pw" not in label and "someone" not in label
