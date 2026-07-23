"""Focused control-plane security and lifecycle regression tests."""

import unittest

from backend.security import (
    redact_phone_number,
    RateLimiter,
    validate_agent_gender,
    validate_agent_identity,
    validate_e164_phone,
)


class ControlPlaneSecurityTests(unittest.TestCase):
    def test_control_models_reject_invalid_operator_input(self):
        self.assertTrue(validate_e164_phone("+15551234567"))
        self.assertFalse(validate_e164_phone("555-1234"))
        self.assertTrue(validate_agent_identity("agent-1234"))
        self.assertFalse(validate_agent_identity("../../etc/passwd"))
        self.assertTrue(validate_agent_gender("male"))
        self.assertFalse(validate_agent_gender("unknown"))

    def test_phone_redaction_keeps_only_last_four_digits(self):
        self.assertEqual(redact_phone_number("+15551234567"), "***4567")
        self.assertEqual(redact_phone_number(""), "not set")

    def test_rate_limiter_blocks_after_limit(self):
        limiter = RateLimiter(window_seconds=60, max_entries=10)
        self.assertTrue(limiter.allows("127.0.0.1", "api", 2))
        self.assertTrue(limiter.allows("127.0.0.1", "api", 2))
        self.assertFalse(limiter.allows("127.0.0.1", "api", 2))


if __name__ == "__main__":
    unittest.main()
