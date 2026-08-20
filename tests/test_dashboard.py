from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import metric_catalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def synthetic_snapshot(projects: int = 50, days: int = 365, models: int = 12, specs: int = 60) -> dict:
    end = dt.date(2026, 8, 20)
    daily = []
    cost_daily = []
    measurement_daily = []
    project_rows = []
    total_tokens = 0
    total_cost = 0.0
    for project in range(projects):
        project_rows.append({"project_id": f"proj-{project:08d}", "project_code": f"proj-{project:08d}"})
    for offset in range(days):
        day = (end - dt.timedelta(days=days - 1 - offset)).isoformat()
        by_model = {}
        for model in range(models):
            value = (offset + 1) * (model + 2) * 100
            by_model[f"model-{model:02d}"] = {"tokens": value, "usd": value / 1_000_000, "unpriced_tokens": 0}
        cost_daily.append({"date": day, "vendors": {"anthropic": {"by_model": by_model}, "openai": {"by_model": {}}}})
        measurement_daily.append({"date": day, "sources": {"root": {"status_counts": {"ok": 1}}}, "latest_gaps": []})
        for project in range(projects):
            tokens = (offset + 1) * (project + 1) * 10
            cost = tokens / 1_000_000
            total_tokens += tokens
            total_cost += cost
            daily.append(
                {
                    "date": day,
                    "project_id": f"proj-{project:08d}",
                    "vendor": "anthropic" if project % 2 == 0 else "openai",
                    "host_os": "wsl" if project % 3 else "windows",
                    "sessions": 1,
                    "tokens": tokens,
                    "cost_usd": cost,
                    "unpriced_tokens": 0,
                }
            )
    rounds = []
    for spec in range(specs):
        for round_number in range(1, 13):
            ended = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc) + dt.timedelta(hours=spec * 13 + round_number)
            rounds.append(
                {
                    "spec": f"feature-{spec:03d}",
                    "round": round_number,
                    "ended_at": ended.isoformat(),
                    "duration_minutes": float(round_number),
                    "accepted": round_number == 12,
                    "verdict": "ACCEPT" if round_number == 12 else "NOT_ACCEPTED",
                    "findings": round_number % 4,
                    "total_tokens": round_number * 1000,
                    "total_usd": round_number / 10,
                    "unpriced_tokens": 0,
                }
            )
    model_rows = {}
    weight_total = sum(range(1, models + 1))
    allocated = 0
    for model in range(models):
        tokens = total_tokens - allocated if model == models - 1 else total_tokens * (model + 1) // weight_total
        allocated += tokens
        model_rows[f"model-{model:02d}"] = {"sessions": 1, "tokens": tokens, "cost_usd": tokens / 1_000_000, "unpriced_tokens": 0}
    return {
        "schema_version": 2,
        "generated_at": "2026-08-20T12:00:00+00:00",
        "collection": {"date": end.isoformat()},
        "metrics": {
            "observatory": {
                "totals": {"sessions": projects, "tokens": total_tokens, "cost_usd": total_cost, "unpriced_tokens": 0},
                "by_vendor": {
                    "anthropic": {"sessions": projects // 2, "tokens": total_tokens // 2, "cost_usd": total_cost / 2},
                    "openai": {"sessions": projects - projects // 2, "tokens": total_tokens - total_tokens // 2, "cost_usd": total_cost / 2},
                },
                "by_host_os": {
                    "wsl": {"sessions": projects, "tokens": total_tokens * 2 // 3, "cost_usd": total_cost * 2 / 3},
                    "windows": {"sessions": projects // 3, "tokens": total_tokens - total_tokens * 2 // 3, "cost_usd": total_cost / 3},
                },
                "by_model": model_rows,
                "projects": project_rows,
                "daily": daily,
                "unregistered_candidates": {"count": projects},
                "source_roots": [
                    {"root_id": f"root-{index}", "vendor": "anthropic" if index < 2 else "openai", "host_os": "wsl" if index % 2 == 0 else "windows", "status": "ok", "files": 10, "error_files": index, "last_success_at": "2026-08-20T11:30:00+00:00"}
                    for index in range(4)
                ],
                "reconciliation": {"status": "ok"},
                "store": {"integrity": "ok"},
            },
            "cost": {"daily": cost_daily},
            "ledger": {"rounds": rounds},
            "time_v2": {"anomalies": 0},
            "measurement": {"daily": measurement_daily},
            "reliability": {
                "status": "ok",
                "checks": [{"name": "fixture", "status": "ok", "detail": "bounded"}],
                "cadence": {"missed_intervals": 0, "longest_gap_minutes": 0},
                "disk": {"runway_years": 20},
            },
            "now": {"current_state": "idle", "publish_status": "success"},
        },
    }


class DashboardEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = json.loads((PROJECT_ROOT / "data" / "telemetry.json").read_text(encoding="utf-8"))

    def test_catalog_is_bidirectionally_complete_and_page_payload_meets_target(self) -> None:
        page = metric_catalog.build_page_envelope(self.snapshot)
        payload = metric_catalog.page_payload_text(page)
        script = (PROJECT_ROOT / "dashboard.js").read_text(encoding="utf-8")
        self.assertLess(len(payload.encode("utf-8")), metric_catalog.PAGE_TARGET_BYTES)
        self.assertEqual(
            {row["metric_id"] for row in page["catalog"] if row["surface"] == "page"},
            metric_catalog.PAGE_METRIC_IDS,
        )
        for metric_id in metric_catalog.PAGE_METRIC_IDS:
            self.assertIn(f'"{metric_id}"', script, metric_id)
        for row in page["catalog"]:
            self.assertTrue(row["definition"])
            self.assertTrue(row["derivation"])
            self.assertTrue(row["sources"])
            self.assertTrue(row["caveats"])

    def test_windows_and_other_rollups_are_exact_and_bounded(self) -> None:
        page = metric_catalog.build_page_envelope(self.snapshot)
        self.assertEqual(list(page["windows"]), ["7", "30", "90", "all"])
        for window in page["windows"].values():
            self.assertLessEqual(len(window["daily"]), metric_catalog.MAX_TREND_POINTS)
            self.assertLessEqual(len(window["rounds_by_day"]), metric_catalog.MAX_TREND_POINTS)
            self.assertLessEqual(len(window["top_projects"]), metric_catalog.TOP_N + 1)
            self.assertLessEqual(len(window["top_specs"]), metric_catalog.TOP_N + 1)
            self.assertLessEqual(len(window["recent_specs"]), metric_catalog.TOP_N)
            self.assertEqual(sum(int(row["tokens"]) for row in window["top_projects"]), window["summary"]["tokens"])
            self.assertAlmostEqual(sum(float(row["cost_usd"]) for row in window["top_specs"]), window["outcomes"]["cost_usd"], places=5)
            for rows, key in ((window["top_projects"], "tokens"), (window["top_specs"], "cost_usd")):
                if len(rows) == metric_catalog.TOP_N + 1:
                    self.assertEqual(rows[-1]["label"], "other")
                    self.assertGreater(rows[-1]["other_count"], 0)
                    self.assertGreaterEqual(rows[-1][key], 0)
        point = page["point_in_time"]
        self.assertEqual(sum(int(row["tokens"]) for row in point["top_models"]), point["totals"]["tokens"])

    def test_headline_comparisons_use_immediately_prior_equal_utc_windows(self) -> None:
        snapshot = synthetic_snapshot()
        page = metric_catalog.build_page_envelope(snapshot)
        raw = snapshot["metrics"]["observatory"]["daily"]
        for key in ("7", "30", "90"):
            current = page["windows"][key]
            prior = current["comparison"]
            self.assertEqual(prior["to"], metric_catalog._add_days(current["from"], -1))
            self.assertEqual(
                (dt.date.fromisoformat(prior["to"]) - dt.date.fromisoformat(prior["from"])).days + 1,
                current["inclusive_days"],
            )
            expected = sum(row["tokens"] for row in raw if prior["from"] <= row["date"] <= prior["to"])
            self.assertEqual(prior["summary"]["tokens"], expected)
        self.assertIsNone(page["windows"]["all"]["comparison"])

    def test_bucket_totals_survive_top_n_and_utc_boundaries_are_exact(self) -> None:
        snapshot = synthetic_snapshot()
        latest = snapshot["collection"]["date"]
        snapshot["metrics"]["observatory"]["daily"].extend(
            [
                {"date": latest, "project_id": "ad-hoc", "vendor": "openai", "host_os": "wsl", "sessions": 1, "tokens": 1, "cost_usd": 0, "unpriced_tokens": 0},
                {"date": latest, "project_id": "remote", "vendor": "openai", "host_os": "wsl", "sessions": 1, "tokens": 2, "cost_usd": 0, "unpriced_tokens": 0},
            ]
        )
        page = metric_catalog.build_page_envelope(snapshot)
        window = page["windows"]["30"]
        self.assertEqual(window["bucket_tokens"], {"ad-hoc": 1, "remote": 2})
        self.assertNotIn("ad-hoc", [row["label"] for row in window["top_projects"][:-1]])
        self.assertEqual(metric_catalog._day("2026-08-01T23:30:00-02:00"), "2026-08-02")
        self.assertEqual(metric_catalog._day("2026-08-02T00:00:00Z"), "2026-08-02")

    def test_high_cardinality_fixture_has_same_at_rest_shape_and_small_payload(self) -> None:
        real_page = metric_catalog.build_page_envelope(self.snapshot)
        large_page = metric_catalog.build_page_envelope(synthetic_snapshot())
        self.assertEqual(metric_catalog.surface_signature(real_page), metric_catalog.surface_signature(large_page))
        self.assertLess(len(metric_catalog.page_payload_text(large_page).encode("utf-8")), metric_catalog.PAGE_TARGET_BYTES)
        self.assertEqual(len(large_page["windows"]["30"]["top_projects"]), metric_catalog.TOP_N + 1)
        self.assertEqual(len(large_page["point_in_time"]["top_models"]), metric_catalog.TOP_N + 1)
        self.assertEqual(len(large_page["windows"]["30"]["recent_specs"]), metric_catalog.TOP_N)

    def test_catalog_pins_provider_cost_and_duration_traps(self) -> None:
        rows = {row["metric_id"]: row for row in metric_catalog.catalog_rows()}
        lifetime = rows["lifetime_tokens"]["derivation"]
        self.assertIn("cached_input is already inside input", lifetime)
        self.assertIn("reasoning_output is already inside output", lifetime)
        self.assertIn("cache_write_5m", lifetime)
        self.assertIn("cache_write_1h", lifetime)
        duration = rows["median_round_minutes"]["derivation"]
        self.assertIn("clamped to [0, 2,880]", duration)
        self.assertIn("anomaly count", duration)
        self.assertIn("clamp", rows["duration_anomaly_count"]["derivation"])
        script = (PROJECT_ROOT / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotRegex(script, r'\["[^"]+",true\]')

    def test_root_freshness_field_and_multiseries_legends_are_truthful(self) -> None:
        page = metric_catalog.build_page_envelope(synthetic_snapshot())
        self.assertTrue(all(row["last_success_at"] == "2026-08-20T11:30:00+00:00" for row in page["point_in_time"]["roots"]))
        script = (PROJECT_ROOT / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn("item.formatter(maximum)", script)
        self.assertNotIn("item.formatter(seriesMaximum)", script)
        self.assertNotIn("bucket max", script)
        self.assertIn("tokens per bucket", script)
        self.assertIn("median minutes", script)
        self.assertIn("Last successful scan", script)
        self.assertNotIn('["Last successful scan",false]', script)
        self.assertNotIn('["Latest",false]]', script)

    def test_stale_publish_fixture_survives_into_lazy_diagnostics(self) -> None:
        snapshot = synthetic_snapshot()
        snapshot["metrics"]["reliability"]["status"] = "warn"
        snapshot["metrics"]["reliability"]["checks"] = [
            {"name": "publish", "status": "warn", "detail": "last_success_age_hours_25"}
        ]
        page = metric_catalog.build_page_envelope(snapshot)
        self.assertEqual(
            page["point_in_time"]["doctor"]["checks"],
            [{"name": "publish", "status": "warn", "detail": "last_success_age_hours_25"}],
        )
        script = (PROJECT_ROOT / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("(point.doctor || {}).checks", script)
        self.assertIn("esc(row.detail)", script)

    def test_generated_page_reconciles_with_machine_days_rounds_and_sessions(self) -> None:
        text = (PROJECT_ROOT / "data" / "telemetry.js").read_text(encoding="utf-8")
        page = json.loads(text.removeprefix("window.TELEMETRY=").strip().removesuffix(";"))
        days = [json.loads(line) for line in (PROJECT_ROOT / "data" / "machine" / "days.jsonl").read_text(encoding="utf-8").splitlines()]
        rounds = [json.loads(line) for line in (PROJECT_ROOT / "data" / "machine" / "rounds.jsonl").read_text(encoding="utf-8").splitlines()]
        sessions = [json.loads(line) for line in (PROJECT_ROOT / "data" / "machine" / "sessions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(page["point_in_time"]["totals"]["sessions"], len(sessions))
        self.assertEqual(page["point_in_time"]["totals"]["tokens"], sum(row["tokens"] for row in sessions))
        self.assertEqual(page["point_in_time"]["totals"]["unpriced_tokens"], sum(row["unpriced_tokens"] for row in sessions))
        for window in page["windows"].values():
            selected_days = [row for row in days if window["from"] <= row["date"] <= window["to"]]
            selected_rounds = [row for row in rounds if window["from"] <= metric_catalog._day(row["ended_at"]) <= window["to"]]
            self.assertEqual(window["summary"]["tokens"], sum(row["tokens"] for row in selected_days))
            self.assertAlmostEqual(window["summary"]["cost_usd"], sum(row["api_equivalent_cost_usd"] for row in selected_days), places=5)
            self.assertEqual(window["summary"]["session_days"], sum(row["sessions"] for row in selected_days))
            self.assertEqual(window["outcomes"]["rounds"], len(selected_rounds))
            self.assertAlmostEqual(window["outcomes"]["cost_usd"], sum(row["api_equivalent_cost_usd"] for row in selected_rounds), places=5)


if __name__ == "__main__":
    unittest.main()
