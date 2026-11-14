"""Rate limits on login and registration, kept in memory. The clock is
fake where time matters; no test waits."""

import pytest

from app.core import rate_limits
from app.core.config import settings
from app.core.rate_limits import SlidingWindow

PASSWORD = "correct-horse-battery"


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rate_limits.failed_logins, "clock", clock)
    monkeypatch.setattr(rate_limits.registrations, "clock", clock)
    return clock


@pytest.fixture
def limits(monkeypatch):
    def _limits(logins=10, registrations=5, trust_proxy=False):
        monkeypatch.setattr(settings, "login_attempts_per_15_min", logins)
        monkeypatch.setattr(settings, "registrations_per_hour", registrations)
        monkeypatch.setattr(settings, "trust_proxy_headers", trust_proxy)

    return _limits


@pytest.fixture
def login(client):
    def _login(email, password="wrong-password", forwarded_for=None):
        headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
        return client.post("/auth/login", json={"email": email, "password": password}, headers=headers)

    return _login


@pytest.fixture
def register_from(client):
    def _register(email, forwarded_for=None):
        headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
        return client.post("/auth/register", json={"email": email, "password": PASSWORD}, headers=headers)

    return _register


def assert_too_many(response, minutes=15):
    assert response.status_code == 429
    assert response.json() == {"error": {
        "code": "too_many_attempts",
        "message": f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
    }}
    assert (minutes - 1) * 60 < int(response.headers["Retry-After"]) <= minutes * 60


# --- Login -------------------------------------------------------------------------------------------

def test_the_default_limits_are_the_specs():
    from app.core.config import Settings

    defaults = Settings.model_fields
    assert defaults["login_attempts_per_15_min"].default == 10
    assert defaults["registrations_per_hour"].default == 5
    assert defaults["trust_proxy_headers"].default is False


def test_failed_logins_for_one_email_are_limited_then_429_with_retry_after(register, login, limits, clock):
    limits(logins=10)
    register("alice@example.com", PASSWORD)

    for _ in range(10):
        assert login("alice@example.com").status_code == 401

    assert_too_many(login("alice@example.com"))
    # Once limited, even the right password waits
    assert_too_many(login("alice@example.com", PASSWORD))
    # Upper case is the same email
    assert_too_many(login("ALICE@example.com", PASSWORD))


def test_the_429_never_says_whether_the_email_exists(register, login, limits, clock, monkeypatch):
    limits(logins=2, trust_proxy=True)  # separate IP addresses, so only the email limit applies
    register("alice@example.com", PASSWORD)

    for n in range(2):
        login("alice@example.com", forwarded_for=f"203.0.113.{n}")
        login("nobody@example.com", forwarded_for=f"198.51.100.{n}")

    known, unknown = login("alice@example.com", forwarded_for="192.0.2.1"), login("nobody@example.com", forwarded_for="192.0.2.2")
    assert known.json() == unknown.json()
    assert known.headers["Retry-After"] == unknown.headers["Retry-After"]
    assert_too_many(known)


def test_a_successful_login_does_not_count(register, login, limits, clock):
    limits(logins=3)
    register("alice@example.com", PASSWORD)

    for _ in range(2):
        assert login("alice@example.com").status_code == 401
    for _ in range(5):
        assert login("alice@example.com", PASSWORD).status_code == 200

    assert login("alice@example.com").status_code == 401  # the third failure
    assert_too_many(login("alice@example.com", PASSWORD))


def test_failed_logins_from_one_ip_address_are_limited_across_emails(login, limits, clock):
    limits(logins=3)

    for n in range(3):
        assert login(f"user{n}@example.com").status_code == 401

    assert_too_many(login("someone-else@example.com"))


def test_the_limit_lifts_when_the_oldest_failure_is_15_minutes_old(register, login, limits, clock):
    limits(logins=2)
    register("alice@example.com", PASSWORD)
    login("alice@example.com")
    clock.now += 300
    login("alice@example.com")

    limited = login("alice@example.com", PASSWORD)
    assert_too_many(limited, minutes=10)
    assert limited.headers["Retry-After"] == "600"  # the first failure expires in 10 minutes

    clock.now += 600
    assert login("alice@example.com", PASSWORD).status_code == 200


# --- Registration ------------------------------------------------------------------------------------

def test_registrations_from_one_ip_address_are_limited_per_hour(register_from, limits, clock):
    limits(registrations=5)

    for n in range(5):
        assert register_from(f"user{n}@example.com").status_code == 201

    assert_too_many(register_from("user5@example.com"), minutes=60)
    clock.now += 3600
    assert register_from("user5@example.com").status_code == 201


def test_a_registration_for_a_taken_email_counts_too(register_from, limits, clock):
    limits(registrations=2)

    assert register_from("alice@example.com").status_code == 201
    assert register_from("alice@example.com").status_code == 409

    assert_too_many(register_from("bob@example.com"), minutes=60)


# --- The client's IP address ---------------------------------------------------------------------------

def test_x_forwarded_for_is_ignored_unless_trusted(register_from, login, limits, clock):
    limits(logins=2, registrations=1, trust_proxy=False)

    # Different X-Forwarded-For values, but one connection: one IP address
    login("a@example.com", forwarded_for="203.0.113.1")
    login("b@example.com", forwarded_for="203.0.113.2")
    assert_too_many(login("c@example.com", forwarded_for="203.0.113.3"))
    assert register_from("d@example.com", forwarded_for="203.0.113.4").status_code == 201
    assert_too_many(register_from("e@example.com", forwarded_for="203.0.113.5"), minutes=60)


def test_x_forwarded_for_is_used_when_trusted(register_from, login, limits, clock):
    limits(logins=2, registrations=1, trust_proxy=True)

    login("a@example.com", forwarded_for="203.0.113.1")
    login("b@example.com", forwarded_for="203.0.113.1")
    assert_too_many(login("c@example.com", forwarded_for="203.0.113.1"))
    assert login("c@example.com", forwarded_for="203.0.113.2").status_code == 401  # another address
    assert register_from("d@example.com", forwarded_for="203.0.113.4").status_code == 201
    assert register_from("e@example.com", forwarded_for="203.0.113.5").status_code == 201


def test_only_the_last_x_forwarded_for_address_is_trusted(login, limits, clock):
    # Earlier entries are whatever the client sent; the last is what our proxy saw
    limits(logins=1, trust_proxy=True)

    login("a@example.com", forwarded_for="1.1.1.1, 203.0.113.9")
    assert_too_many(login("b@example.com", forwarded_for="2.2.2.2, 203.0.113.9"))


# --- The window itself ---------------------------------------------------------------------------------

def test_a_sliding_window_counts_recent_events_per_key():
    clock = Clock()
    window = SlidingWindow(60, clock=clock)

    window.record(["a"])
    clock.now += 30
    window.record(["a", "b"])

    assert window.retry_after(["a"], limit=3) is None
    assert window.retry_after(["a"], limit=2) == 30  # the first event expires in 30 seconds
    assert window.retry_after(["b"], limit=1) == 60
    assert window.retry_after(["a", "b"], limit=1) == 60  # the longest wait of any key at its limit
    clock.now += 60
    assert window.retry_after(["a", "b"], limit=1) is None
