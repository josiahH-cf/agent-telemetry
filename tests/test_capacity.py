from __future__ import annotations

import datetime as dt
import unittest

import collect


UTC = dt.timezone.utc


class CapacityNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def test_claude_freshness_is_per_window_and_uses_configured_age(self) -> None:
        usage = {
            "claude_usage_snapshot": {
                "source": "claude_slash_usage_local_snapshot",
                "provider": "anthropic",
                "observed_at": "2026-08-21T11:30:00+00:00",
                "capture_status": "automatic_success",
                "quota_windows": [
                    {"window": "five_hour", "used_percent": 20, "remaining_percent": 80, "resets_at": "2026-08-21T11:59:00+00:00"},
                    {"window": "seven_day", "used_percent": 40, "remaining_percent": 60, "resets_at": "2026-08-25T12:00:00+00:00"},
                ],
            }
        }
        value = collect.build_usage_left({}, usage, now=self.now, claude_max_age_seconds=3600)
        windows = {row["window"]: row for row in value["anthropic"]["quota_windows"]}
        self.assertEqual(windows["five_hour"]["freshness_status"], "stale")
        self.assertEqual(windows["seven_day"]["freshness_status"], "available")
        self.assertEqual(windows["seven_day"]["display_label"], "Seven-day window")
        self.assertEqual(value["anthropic"]["freshness_max_age_hours"], 1.0)

    def test_capture_failure_with_last_good_is_retained_not_current(self) -> None:
        usage = {
            "claude_usage_snapshot": {
                "source": "claude_slash_usage_local_snapshot",
                "provider": "anthropic",
                "observed_at": "2026-08-21T11:45:00+00:00",
                "capture_status": "automatic_timeout",
                "quota_windows": [
                    {"window": "five_hour", "used_percent": 25, "remaining_percent": 75, "resets_at": "2026-08-21T15:00:00+00:00"}
                ],
            }
        }
        row = collect.build_usage_left({}, usage, now=self.now, claude_max_age_seconds=3600)["anthropic"]["quota_windows"][0]
        self.assertEqual(row["freshness_status"], "retained_last_good")
        self.assertEqual(row["capture_status"], "automatic_timeout")

    def test_openai_two_hour_boundary_and_invalid_percentages(self) -> None:
        usage = {
            "rate_limits": {
                "observed_at": "2026-08-21T09:00:00+00:00",
                "primary": {"used_percent": 25, "remaining_percent": 75, "window_minutes": 300, "resets_at": "2026-08-22T09:00:00+00:00"},
                "secondary": {"used_percent": 150, "remaining_percent": -50, "window_minutes": 10080},
            }
        }
        value = collect.build_usage_left({}, usage, now=self.now)
        self.assertEqual(len(value["openai"]["quota_windows"]), 1)
        self.assertEqual(value["openai"]["quota_windows"][0]["freshness_status"], "stale")
        self.assertEqual(value["openai"]["quota_windows"][0]["display_label"], "Primary window")

    def test_unknown_valid_quota_windows_survive_the_verbose_envelope(self) -> None:
        extra_windows = {
            f"window_{index:02d}": {
                "used_percent": index,
                "remaining_percent": 100 - index,
                "window_minutes": 60 * (index + 1),
            }
            for index in range(20)
        }
        usage = {
            "rate_limits": {
                "observed_at": "2026-08-21T11:30:00+00:00",
                "primary": {"used_percent": 25, "remaining_percent": 75, "window_minutes": 300},
                "monthly": {"used_percent": 10, "remaining_percent": 90, "window_minutes": 43200},
                **extra_windows,
            }
        }
        value = collect.build_usage_left({}, usage, now=self.now)["openai"]
        self.assertEqual(
            [row["window"] for row in value["quota_windows"]],
            ["primary", "monthly", *sorted(extra_windows)],
        )

    def test_future_observation_is_stale_but_post_reset_observation_is_current(self) -> None:
        future = collect.normalize_quota_provider(
            {
                "source": "rollout_token_count",
                "observed_at": "2026-08-21T12:05:00+00:00",
                "quota_windows": [
                    {"window": "primary", "remaining_percent": 75, "resets_at": "2026-08-21T14:00:00+00:00"}
                ],
            },
            provider="openai",
            now=self.now,
            max_age_seconds=7200,
        )
        newer_than_reset = collect.normalize_quota_provider(
            {
                "source": "rollout_token_count",
                "observed_at": "2026-08-21T11:00:00+00:00",
                "quota_windows": [
                    {"window": "primary", "remaining_percent": 75, "resets_at": "2026-08-21T10:00:00+00:00"}
                ],
            },
            provider="openai",
            now=self.now,
            max_age_seconds=7200,
        )
        self.assertEqual(future["quota_windows"][0]["freshness_status"], "stale")
        self.assertEqual(newer_than_reset["quota_windows"][0]["freshness_status"], "available")

    def test_openai_timeout_with_cached_limits_is_retained_last_good(self) -> None:
        usage = {
            "sources": {
                "openai_usage": {
                    "status": "timeout",
                    "skips": [{"reason": "source_timeout_cached_last_good", "count": 1}],
                }
            },
            "rate_limits": {
                "observed_at": "2026-08-21T11:30:00+00:00",
                "primary": {"used_percent": 25, "remaining_percent": 75, "window_minutes": 300},
            },
        }
        provider = collect.build_usage_left({}, usage, now=self.now)["openai"]
        self.assertEqual(provider["quota_status"], "retained_last_good")
        self.assertEqual(provider["capture_status"], "source_timeout_cached_last_good")
        self.assertEqual(provider["quota_windows"][0]["freshness_status"], "retained_last_good")

    def test_claude_age_boundary_and_invalid_config_use_the_capture_contract(self) -> None:
        usage = {
            "claude_usage_snapshot": {
                "source": "claude_slash_usage_local_snapshot",
                "provider": "anthropic",
                "observed_at": "2026-08-21T11:00:00+00:00",
                "capture_status": "automatic_success",
                "quota_windows": [{"window": "five_hour", "remaining_percent": 75}],
            }
        }
        configured = collect.configured_claude_quota_max_age_seconds(
            {"claude_usage_capture": {"max_cache_age_seconds": 3600}}
        )
        invalid = collect.configured_claude_quota_max_age_seconds(
            {"claude_usage_capture": {"max_cache_age_seconds": 7200}}
        )
        row = collect.build_usage_left({}, usage, now=self.now, claude_max_age_seconds=configured)["anthropic"]["quota_windows"][0]
        self.assertEqual(row["freshness_status"], "stale")
        self.assertEqual(invalid, collect.CLAUDE_QUOTA_DEFAULT_MAX_AGE_SECONDS)

    def test_failed_capture_without_usable_value_is_error(self) -> None:
        usage = {
            "claude_usage_snapshot": {
                "source": "claude_slash_usage_local_snapshot",
                "provider": "anthropic",
                "observed_at": "2026-08-21T11:45:00+00:00",
                "capture_status": "automatic_command_failed",
                "quota_windows": [{"window": "five_hour", "used_percent": 125, "remaining_percent": -25}],
            }
        }
        provider = collect.build_usage_left({}, usage, now=self.now)["anthropic"]
        self.assertEqual(provider["quota_status"], "error")
        self.assertEqual(provider["quota_windows"], [])

    def test_failed_capture_value_without_observation_is_not_last_good(self) -> None:
        usage = {
            "claude_usage_snapshot": {
                "source": "claude_slash_usage_local_snapshot",
                "provider": "anthropic",
                "capture_status": "automatic_timeout",
                "quota_windows": [{"window": "five_hour", "used_percent": 25, "remaining_percent": 75}],
            }
        }
        provider = collect.build_usage_left({}, usage, now=self.now)["anthropic"]
        self.assertEqual(provider["quota_status"], "error")
        self.assertEqual(provider["quota_windows"], [])

    def test_openai_adapter_error_marks_provider_snapshot_as_retained(self) -> None:
        provider_snapshot = {
            "snapshot": {"generated_at": "2026-08-21T11:30:00+00:00", "age_hours": 0.5},
            "providers": [
                {
                    "provider": "codex",
                    "remaining_status": "available",
                    "remaining_percent": 75,
                    "quota_status": "available",
                    "quota_windows": [
                        {"window": "primary", "used_percent": 25, "remaining_percent": 75, "window_minutes": 300}
                    ],
                }
            ],
        }
        usage = {"sources": {"openai_usage": {"status": "error", "skips": [{"reason": "usage_adapter_error", "count": 1}]}}}
        result = collect.build_usage_left(provider_snapshot, usage, now=self.now)["openai"]
        self.assertEqual(result["capture_status"], "source_error")
        self.assertEqual(result["quota_status"], "retained_last_good")
        self.assertEqual(result["quota_windows"][0]["freshness_status"], "retained_last_good")


if __name__ == "__main__":
    unittest.main()
