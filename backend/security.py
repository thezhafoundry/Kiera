"""Small dependency-free security helpers shared by the control plane and tests."""

import re
import time


_E164_RE = re.compile(r"^\+\d{8,15}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_e164_phone(value: str) -> bool:
    return bool(_E164_RE.fullmatch(value or ""))


def validate_agent_identity(value: str) -> bool:
    return bool(_IDENTITY_RE.fullmatch(value or "")) and "agent" in value.lower()


def validate_agent_gender(value: str) -> bool:
    return value in {"male", "female"}


def redact_phone_number(value: str) -> str:
    if not value:
        return "not set"
    return f"***{value[-4:]}" if len(value) >= 4 else "***"


class RateLimiter:
    """Small in-process fixed-window limiter for a single backend instance."""

    def __init__(self, window_seconds: float = 60.0, max_entries: int = 4096):
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._state: dict[tuple[str, str], tuple[float, int]] = {}

    def allows(self, client_key: str, bucket: str, limit: int) -> bool:
        now = time.monotonic()
        key = (client_key, bucket)
        started, count = self._state.get(key, (now, 0))
        if now - started >= self.window_seconds:
            started, count = now, 0
        count += 1
        self._state[key] = (started, count)
        if len(self._state) > self.max_entries:
            oldest = min(self._state, key=lambda item: self._state[item][0])
            self._state.pop(oldest, None)
        return count <= limit
