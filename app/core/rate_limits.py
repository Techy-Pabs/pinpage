"""Rate limits on login and registration, kept in memory.

- Login: at most LOGIN_ATTEMPTS_PER_15_MIN failed attempts per email and
  per IP address in any 15 minutes. Then 429 too_many_attempts, with
  Retry-After, until the oldest failure is 15 minutes old. A successful
  login doesn't count.
- Registration: at most REGISTRATIONS_PER_HOUR attempts per IP address.

In memory, which works because the app runs one worker. Before running
several workers (or several instances), this must move to a shared store
such as Redis, or each worker would allow the full limit.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request, status

from app.core.config import settings
from app.core.errors import ApiError

LOGIN_WINDOW_SECONDS = 15 * 60
REGISTRATION_WINDOW_SECONDS = 60 * 60
# Above this many keys, keys whose events have all expired are dropped
SWEEP_ABOVE_KEYS = 10_000


class SlidingWindow:
    """Event times per key over the last window_seconds, thread safe."""

    def __init__(self, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.window = window_seconds
        self.clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            return deque()
        while events and events[0] <= now - self.window:
            events.popleft()
        if not events:
            del self._events[key]
        return events

    def retry_after(self, keys: list[str], limit: int) -> int | None:
        """Seconds until any key at its limit frees a slot, or None if no
        key is at its limit."""
        with self._lock:
            now = self.clock()
            waits = []
            for key in keys:
                events = self._recent(key, now)
                if len(events) >= limit:
                    # Free once enough of the oldest events have expired
                    waits.append(events[len(events) - limit] + self.window - now)
            return max(1, math.ceil(max(waits))) if waits else None

    def record(self, keys: list[str]) -> None:
        with self._lock:
            now = self.clock()
            if len(self._events) > SWEEP_ABOVE_KEYS:
                for key in list(self._events):
                    self._recent(key, now)
            for key in keys:
                self._events.setdefault(key, deque()).append(now)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


failed_logins = SlidingWindow(LOGIN_WINDOW_SECONDS)
registrations = SlidingWindow(REGISTRATION_WINDOW_SECONDS)


def client_ip(request: Request) -> str:
    """The connection's address, or, only when TRUST_PROXY_HEADERS is true
    (behind a proxy that adds it), the last address in X-Forwarded-For: the
    one our proxy saw. Earlier entries are whatever the client sent."""
    if settings.trust_proxy_headers:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def too_many_attempts(retry_after: int) -> ApiError:
    # The same message whatever the reason, so it never says whether an
    # email has an account
    minutes = max(1, math.ceil(retry_after / 60))
    return ApiError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "too_many_attempts",
        f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
        headers={"Retry-After": str(retry_after)},
    )


def login_keys(email: str, ip: str) -> list[str]:
    return [f"email:{email.lower()}", f"ip:{ip}"]


def check_login(email: str, ip: str) -> None:
    """Raises 429 if this email or this IP address had too many failed
    logins in the last 15 minutes."""
    wait = failed_logins.retry_after(login_keys(email, ip), settings.login_attempts_per_15_min)
    if wait is not None:
        raise too_many_attempts(wait)


def record_failed_login(email: str, ip: str) -> None:
    failed_logins.record(login_keys(email, ip))


def check_and_record_registration(ip: str) -> None:
    """Raises 429 if this IP address registered too often in the last hour;
    otherwise counts this attempt."""
    keys = [f"ip:{ip}"]
    wait = registrations.retry_after(keys, settings.registrations_per_hour)
    if wait is not None:
        raise too_many_attempts(wait)
    registrations.record(keys)
