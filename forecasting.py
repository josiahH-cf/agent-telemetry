"""Metadata-only, read-only phase forecasts and immutable admission comparisons.

Collection retains capacity and receipt metadata in the restricted canonical store.
Consumer requests fit deterministic completed-phase medians; they never collect,
write, probe a provider or decide whether execution is allowed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import observatory
import outcome_quality
import usage

CONTRACT = "telemetry-forecasting-v1"
ESTIMATOR = "completed-phase-median-v1"
PRIVATE_KINDS = {"forecast.admitted", "forecast.observed"}
MAX_AGE_SECONDS = 2 * 3600
MAX_COMPARISONS = 100
METRICS = {
    "phase_forecast_tokens": ("tokens", "tokens"),
    "phase_forecast_api_equivalent_usd": ("API-equivalent USD", "api_equivalent_cost_usd"),
    "phase_forecast_unpriced_tokens": ("tokens", "unpriced_tokens"),
    "phase_forecast_allowance": ("percentage points of full allowance window", None),
}
SCHEMA = json.loads((Path(__file__).resolve().parent / "data/schema/forecasting.schema.json").read_text(encoding="utf-8"))
MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS forecast_events (
  event_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, producer TEXT NOT NULL,
  kind TEXT NOT NULL, at TEXT NOT NULL, record_json TEXT NOT NULL, ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_event_conflicts (
  event_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_capacity (
  observation_id TEXT PRIMARY KEY, vendor TEXT NOT NULL, account_id TEXT NOT NULL,
  environment TEXT, window TEXT NOT NULL, used_percent REAL NOT NULL,
  resets_at TEXT, observed_at TEXT NOT NULL, retained_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forecast_capacity_pool ON forecast_capacity(vendor,account_id,window,observed_at);
"""


class MetadataError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _iso(value: dt.datetime | None) -> str | None:
    return observatory.iso(value).replace("+00:00", "Z") if value is not None else None


def _sanitize(value: Any, rule: dict[str, Any]) -> Any:
    """Validate defined metadata recursively, dropping extras before any storage/echo."""
    if "$ref" in rule:
        rule = SCHEMA["$defs"][rule["$ref"].rsplit("/", 1)[-1]]
    types = rule.get("type")
    types = types if isinstance(types, list) else [types]
    if not any(observatory.validate_json_type(value, t) for t in types):
        raise MetadataError("metadata_type_invalid")
    if "const" in rule and value != rule["const"] or "enum" in rule and value not in rule["enum"]:
        raise MetadataError("metadata_value_invalid")
    if isinstance(value, str):
        if rule.get("pattern") and not re.fullmatch(rule["pattern"], value):
            raise MetadataError("metadata_identifier_invalid")
        if "T" in value and rule.get("pattern", "").startswith("[0-9]{4}") and usage.parse_timestamp(value) is None:
            raise MetadataError("metadata_timestamp_invalid")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < rule.get("minimum", -math.inf) or value > rule.get("maximum", math.inf):
            raise MetadataError("metadata_number_invalid")
    if isinstance(value, list):
        if len(value) < rule.get("minItems", 0) or len(value) > rule.get("maxItems", len(value)):
            raise MetadataError("metadata_cardinality_invalid")
        return [_sanitize(v, rule["items"]) for v in value]
    if isinstance(value, dict):
        if any(k not in value for k in rule.get("required", [])):
            raise MetadataError("metadata_required_missing")
        return {k: _sanitize(value[k], v) for k, v in rule.get("properties", {}).items() if k in value}
    return value


def includes(request: dict[str, Any]) -> list[str]:
    return ["features", "verification", "ordinary-repair"] if request["phase"] == "implementation" else ["selected-phase"]


def request_metadata(value: Any) -> dict[str, Any]:
    request = _sanitize(value, SCHEMA["$defs"]["request"])
    if request["phase"] == "implementation" and request["scope_kind"] != "milestone":
        raise MetadataError("implementation_requires_whole_milestone")
    for key in ("project_id", "workflow_id", "feature_count"):
        request.setdefault(key, None)
    pools = set()
    for choice in request["choices"]:
        for key in ("account_id", "environment", "model", "effort"):
            choice.setdefault(key, None)
        pool = (choice["vendor"], choice["account_id"], choice["environment"])
        if pool in pools:
            raise MetadataError("duplicate_execution_pool")
        pools.add(pool)
    request["choices"].sort(key=lambda c: (c["vendor"], c["account_id"] or "", c["environment"] or ""))
    return request


def forecast_metadata(value: Any) -> dict[str, Any]:
    forecast = _sanitize(value, SCHEMA["$defs"]["forecast"])
    forecast["request"] = request_metadata(forecast["request"])
    if forecast["includes"] != includes(forecast["request"]):
        raise MetadataError("forecast_boundary_invalid")
    seen = set()
    for p in forecast["predictions"]:
        key = p["choice_index"], p["metric_id"], p["window"]
        if key in seen or p["choice_index"] >= len(forecast["request"]["choices"]) or p["unit"] != METRICS[p["metric_id"]][0]:
            raise MetadataError("forecast_prediction_invalid")
        seen.add(key)
        if (p["status"] == "estimated") != (p["estimate"] is not None) or p["estimate"] is not None and (p["basis"]["sample_count"] == 0 or p["reason"] is not None):
            raise MetadataError("forecast_prediction_invalid")
        if (p["lower"] is None) != (p["upper"] is None) or p["lower"] is not None and (p["estimate"] is None or not p["lower"] <= p["estimate"] <= p["upper"]):
            raise MetadataError("forecast_bounds_invalid")
        if p["metric_id"] != "phase_forecast_allowance" and (p["window"] is not None or p["capacity_observation_id"] is not None) or p["metric_id"] == "phase_forecast_allowance" and p["estimate"] is not None and (p["window"] is None or p["capacity_observation_id"] is None):
            raise MetadataError("forecast_window_invalid")
    for index in range(len(forecast["request"]["choices"])):
        if any((index, metric, None) not in seen for metric in METRICS if metric != "phase_forecast_allowance"):
            raise MetadataError("forecast_prediction_missing")
    expected = "fc-" + _digest({k: v for k, v in forecast.items() if k != "forecast_id"})
    if forecast["forecast_id"] != expected:
        raise MetadataError("forecast_digest_mismatch")
    return forecast


def observation_metadata(value: Any) -> dict[str, Any]:
    observation = _sanitize(value, SCHEMA["$defs"]["observation"])
    observation["request"] = request_metadata(observation["request"])
    observation["outcome_ids"] = sorted(set(observation["outcome_ids"]))
    observation.setdefault("forecast_id", None)
    observation.setdefault("allowance_pairs", [])
    if observation["includes"] != includes(observation["request"]):
        raise MetadataError("observation_boundary_incomplete")
    if usage.parse_timestamp(observation["finished_at"]) < usage.parse_timestamp(observation["started_at"]):
        raise MetadataError("observation_bounds_invalid")
    pairs = {(p["before_id"], p["after_id"]) for p in observation["allowance_pairs"]}
    if len(pairs) != len(observation["allowance_pairs"]):
        raise MetadataError("duplicate_allowance_pair")
    return observation


def receipt_metadata(record: dict[str, Any]) -> dict[str, Any]:
    key = "forecast" if record["kind"] == "forecast.admitted" else "observation"
    value = forecast_metadata(record.get(key)) if key == "forecast" else observation_metadata(record.get(key))
    at = usage.parse_timestamp(record["at"])
    if at < usage.parse_timestamp(value["created_at"] if key == "forecast" else value["finished_at"]):
        raise MetadataError("forecast_receipt_time_invalid")
    # Dedicated private kinds carry no legacy free-form fields or nested usage.
    return {k: record[k] for k in ("interface", "producer", "event_id", "kind", "at", "linkage", "evidence_digest")} | {key: value}


def ingest_event(connection: sqlite3.Connection, root_id: str, record: dict[str, Any], now: str) -> str:
    payload = _json(record)
    prior = connection.execute("SELECT record_json FROM forecast_events WHERE event_id=?", (record["event_id"],)).fetchone()
    if prior is not None:
        if prior[0] != payload:
            connection.execute("INSERT OR IGNORE INTO forecast_event_conflicts VALUES(?,?)", (record["event_id"], now))
            return "conflict"
        return "replay"
    connection.execute("INSERT INTO forecast_events VALUES(?,?,?,?,?,?,?)", (record["event_id"], root_id, record["producer"], record["kind"], record["at"], payload, now))
    return "inserted"


def _has_tables(connection: sqlite3.Connection) -> bool:
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_events'").fetchone())


def capacity_metadata(row: dict[str, Any]) -> dict[str, Any] | None:
    used, observed = row.get("used_percent"), usage.parse_timestamp(row.get("observed_at"))
    if not row.get("account_id") or row.get("vendor") not in ("anthropic", "openai") or not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used) or not 0 <= used <= 100 or observed is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._():-]{0,79}", str(row.get("window") or "")):
        return None
    return {"vendor": row["vendor"], "account_id": row["account_id"], "environment": row.get("environment"), "window": row["window"],
        "used_percent": float(used), "resets_at": _iso(usage.parse_timestamp(row.get("resets_at"))), "observed_at": _iso(observed)}


def retain_capacity(connection: sqlite3.Connection, state_root: Path, now: dt.datetime) -> None:
    # Reuse the established capacity parsers and identity; no provider interaction.
    import consumer
    with connection:
        for row in consumer.capacity_view(state_root, connection, now):
            value = capacity_metadata(row)
            if value is not None and usage.parse_timestamp(value["observed_at"]) <= now:
                connection.execute("INSERT OR IGNORE INTO forecast_capacity VALUES(?,?,?,?,?,?,?,?,?)", (_digest(value), *value.values(), _iso(now)))


def capacity_references(connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        value = capacity_metadata(row)
        key = _digest(value) if value else None
        retained = bool(key and _has_tables(connection) and connection.execute("SELECT 1 FROM forecast_capacity WHERE observation_id=?", (key,)).fetchone())
        row["observation_id"] = key if retained else None
    return rows


def preserve_rebuild_history(source: Path, target: sqlite3.Connection) -> None:
    """Rebuild receipts from sources, and retain project-owned immutable evidence.

    A provider's latest quota cache cannot reconstruct earlier observations.
    Copy these private tables read-only before the normal integrity/swap gate.
    """
    if not source.is_file():
        return
    old = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        if not _has_tables(old):
            return
        with target:
            for table in ("forecast_events", "forecast_event_conflicts", "forecast_capacity"):
                for row in old.execute(f"SELECT * FROM {table}"):
                    target.execute(f"INSERT OR IGNORE INTO {table} VALUES({','.join('?' for _ in row)})", tuple(row))
    finally:
        old.close()


def _events(connection: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], Counter]:
    admissions, revisions, reasons = {}, defaultdict(lambda: defaultdict(dict)), Counter()
    conflicts = {r[0] for r in connection.execute("SELECT event_id FROM forecast_event_conflicts")}
    for row in connection.execute("SELECT * FROM forecast_events ORDER BY at,event_id"):
        if row["event_id"] in conflicts:
            reasons["event_conflict"] += 1
            continue
        record = json.loads(row["record_json"])
        if row["kind"] == "forecast.admitted":
            value = record["forecast"]
            admissions.setdefault(value["forecast_id"], {"forecast": value, "admitted_at": _iso(usage.parse_timestamp(row["at"]))})
        else:
            value = record["observation"]
            revisions[value["observation_id"]][value["revision"]][_digest(value)] = value
    observations = []
    for oid in sorted(revisions):
        current = revisions[oid][max(revisions[oid])]
        if len(current) != 1:
            reasons["observation_revision_conflict"] += 1
            continue
        observations.append(next(iter(current.values())))
    return admissions, observations, reasons


def _choice_component(actual: dict[str, Any], choice: dict[str, Any]) -> dict[str, Any] | None:
    matches = [c for c in actual["components"] if c["vendor"] == choice["vendor"]
        and (choice["account_id"] is None or c["account_id"] == choice["account_id"])
        and (choice["environment"] is None or c["environment"] == choice["environment"])]
    return matches[0] if len(matches) == 1 else None


def _allowance(connection: sqlite3.Connection, observation: dict[str, Any], actual: dict[str, Any], pair: dict[str, Any], sessions: set[str]) -> dict[str, Any]:
    result = {"before_id": pair["before_id"], "after_id": pair["after_id"], "status": "unavailable", "reason": None, "value": None, "attribution": "estimated"}
    rows = [connection.execute("SELECT * FROM forecast_capacity WHERE observation_id=?", (pair[k],)).fetchone() for k in ("before_id", "after_id")]
    if any(r is None for r in rows):
        return {**result, "reason": "capacity_observations_missing"}
    before, after = (dict(r) for r in rows)
    result.update({k: before[k] for k in ("vendor", "account_id", "environment", "window")})
    if any(before[k] != after[k] for k in ("vendor", "account_id", "environment", "window")):
        return {**result, "reason": "allowance_pool_changed"}
    start, end = (usage.parse_timestamp(observation[k]) for k in ("started_at", "finished_at"))
    lo, hi, reset = (usage.parse_timestamp(before["observed_at"]), usage.parse_timestamp(after["observed_at"]), usage.parse_timestamp(before["resets_at"]))
    if not lo <= start <= end <= hi or (start-lo).total_seconds() > MAX_AGE_SECONDS or (hi-end).total_seconds() > MAX_AGE_SECONDS:
        return {**result, "reason": "capacity_not_bracketing"}
    if reset is None or before["resets_at"] != after["resets_at"] or hi >= reset or after["used_percent"] < before["used_percent"]:
        return {**result, "reason": "allowance_reset_or_decrease"}
    comp = _choice_component(actual, {k: before[k] for k in ("vendor", "account_id", "environment")})
    if comp is None or comp["shared_session_tokens"] or comp["unallocated_session_tokens"] or pair["account_activity"] != "isolated":
        return {**result, "reason": "account_activity_not_isolated"}
    competing = connection.execute(
        "SELECT DISTINCT u.vendor,u.session_id FROM usage_observations u JOIN source_files f ON f.file_id=u.file_id JOIN source_identities i ON i.source_key='local:'||f.root_id WHERE u.vendor=? AND i.account_id=? AND julianday(u.timestamp_utc)>=julianday(?) AND julianday(u.timestamp_utc)<=julianday(?)",
        (before["vendor"], before["account_id"], before["observed_at"], after["observed_at"]))
    if any(hashlib.sha256(f'{r[0]}:{r[1]}'.encode()).hexdigest() not in sessions for r in competing):
        return {**result, "reason": "competing_account_usage"}
    imported = connection.execute(
        "SELECT DISTINCT u.vendor,u.session_id FROM imported_observations u JOIN source_identities i ON i.source_key=u.source_key WHERE u.vendor=? AND i.account_id=? AND julianday(u.timestamp_utc)>=julianday(?) AND julianday(u.timestamp_utc)<=julianday(?)",
        (before["vendor"], before["account_id"], before["observed_at"], after["observed_at"]))
    if any(hashlib.sha256(f'{r[0]}:{r[1]}'.encode()).hexdigest() not in sessions for r in imported):
        return {**result, "reason": "competing_account_usage"}
    return {**result, "status": "estimated", "value": round(after["used_percent"] - before["used_percent"], 6)}


def _samples(connection: sqlite3.Connection, observations: list[dict[str, Any]], now: dt.datetime) -> tuple[list[dict[str, Any]], Counter]:
    if not observations:
        return [], Counter()
    receipts = outcome_quality._receipts(connection)
    managed = outcome_quality._managed_sessions(connection)
    samples, reasons = [], Counter()
    for observation in observations:
        actual = outcome_quality.phase_usage_union(connection, observation["outcome_ids"], started_at=observation["started_at"], finished_at=observation["finished_at"], managed=managed, receipts=receipts)
        problem = None
        if observation["finish"] != "completed":
            problem = "phase_not_completed"
        elif observation["coverage"] != "complete":
            problem = "phase_coverage_incomplete"
        elif usage.parse_timestamp(observation["finished_at"]) > now:
            problem = "future_observation"
        elif actual["status"] != "exact":
            problem = "usage_incomplete"
        matched = []
        for choice in observation["request"]["choices"]:
            c = _choice_component(actual, choice)
            if c is None or c["tokens"] is None or choice["model"] is not None and c["models"] != [choice["model"]] or choice["effort"] is not None and c["efforts"] != [choice["effort"]]:
                problem = problem or "execution_basis_unverified"
            elif c not in matched:
                matched.append(c)
        if len(matched) != len(actual["components"]):
            problem = problem or "execution_basis_changed"
        units = {u for c in actual["components"] for u in c["evidence_units"]}
        native = {(r['vendor'], r['native_session_id']) for oid in observation['outcome_ids'] for r in receipts[0].get(oid, []) if r.get('vendor') and r.get('native_session_id')}
        sessions = {hashlib.sha256(f'{v}:{s}'.encode()).hexdigest() for v, s in native}
        allowance = [_allowance(connection, observation, actual, p, sessions) for p in observation["allowance_pairs"]]
        sample = {"observation": observation, "actual": actual, "eligible": problem is None, "reason": problem,
            "units": units, "sessions": sessions, "session_units": {sid: value for c in actual["components"] for sid, value in c["evidence_sessions"].items()}, "allowance": allowance}
        sample["digest"] = _digest({"observation": observation, "actual": actual, "allowance": allowance})
        samples.append(sample)
    # Duplicate representations count once; partially overlapping units cannot be
    # independent calibration observations. Disjoint exact turns remain independent.
    canonical, unit_owners, session_owners, whole_sessions = set(), defaultdict(set), defaultdict(set), set()
    for index, sample in enumerate(samples):
        if not sample["eligible"]:
            continue
        key = _digest({"units": sorted(sample["units"]), "context": _cohort_signature(sample["observation"]["request"])})
        if key in canonical:
            sample["eligible"], sample["reason"] = False, "duplicate_usage_observation"
            continue
        canonical.add(key)
        for unit in sample["units"]:
            unit_owners[unit].add(index)
        for sid, units in sample["session_units"].items():
            session_owners[sid].add(index)
            if units == 'whole':
                whole_sessions.add(sid)
    for owners in [*unit_owners.values(), *(session_owners[sid] for sid in whole_sessions)]:
        if len(owners) > 1:
            for index in owners:
                samples[index]["eligible"], samples[index]["reason"] = False, "overlapping_usage_observations"
    for sample in samples:
        if sample["reason"]:
            reasons[sample["reason"]] += 1
    return samples, reasons


def _cohort_signature(request: dict[str, Any]) -> dict[str, Any]:
    return {k: request[k] for k in ("phase", "scope_kind", "workflow_id", "feature_count", "choices")}


def _compatible(request: dict[str, Any], old: dict[str, Any]) -> bool:
    if any(request[k] != old[k] for k in ("phase", "scope_kind")) or len(request["choices"]) != len(old["choices"]):
        return False
    if any(request[k] is not None and request[k] != old[k] for k in ("workflow_id", "feature_count")):
        return False
    return all(a["vendor"] == b["vendor"] and all(a[k] is None or a[k] == b[k] for k in ("account_id", "environment", "model", "effort")) for a, b in zip(request["choices"], old["choices"]))


def _prediction(index: int, metric: str, values: list[tuple[dict[str, Any], float]], cohort: str, *, window: str | None = None, capacity_id: str | None = None, reason: str = "no_qualified_history") -> dict[str, Any]:
    numbers = [v for _, v in values]
    return {"choice_index": index, "metric_id": metric, "unit": METRICS[metric][0], "window": window, "capacity_observation_id": capacity_id,
        "estimate": round(statistics.median(numbers), 6) if numbers else None,
        "lower": round(min(numbers), 6) if len(numbers) > 1 else None, "upper": round(max(numbers), 6) if len(numbers) > 1 else None,
        "status": "estimated" if numbers else "unavailable", "reason": None if numbers else reason,
        "uncertainty": "empirical-range" if len(numbers) > 1 else "single-sample" if numbers else "unavailable",
        "basis": {"sample_count": len(values), "sample_digest": _digest(sorted((s["digest"], v) for s, v in values)), "cohort": cohort if values else "none",
            "observed_from": min((s["observation"]["started_at"] for s, _ in values), default=None), "observed_to": max((s["observation"]["finished_at"] for s, _ in values), default=None)}}


def _forecast(request: dict[str, Any], samples: list[dict[str, Any]], capacity: list[dict[str, Any]], gen: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    chosen = [s for s in samples if s["eligible"] and _compatible(request, s["observation"]["request"])]
    local = [s for s in chosen if request["project_id"] and s["observation"]["request"]["project_id"] == request["project_id"]]
    cohort = "project" if len(local) >= 3 else "cross-project"
    if cohort == "project":
        chosen = local
    predictions = []
    for index, choice in enumerate(request["choices"]):
        for metric, (_, key) in METRICS.items():
            if key is None:
                continue
            values = []
            for sample in chosen:
                comp = _choice_component(sample["actual"], choice)
                value = comp.get(key) if comp else None
                if metric == "phase_forecast_api_equivalent_usd" and comp and comp["unpriced_tokens"] != 0:
                    value = None
                if value is not None:
                    values.append((sample, float(value)))
            predictions.append(_prediction(index, metric, values, cohort, reason="pricing_not_fully_observed" if chosen and key != "tokens" else "no_qualified_history"))
        windows = [c for c in capacity if choice["account_id"] is not None and c.get("account_id") == choice["account_id"] and c["vendor"] == choice["vendor"]
            and (choice["environment"] is None or c.get("environment") == choice["environment"]) and c.get("window")]
        if not windows:
            predictions.append(_prediction(index, "phase_forecast_allowance", [], cohort, reason="account_identity_unknown" if not choice["account_id"] else "capacity_unavailable"))
        for cap in windows[:4]:
            values, reason = [], "no_qualified_allowance_history"
            if cap.get("status") != "current" or cap.get("reset_passed"):
                reason = "capacity_stale"
            elif not cap.get("observation_id"):
                reason = "capacity_not_retained"
            else:
                for sample in chosen:
                    matching = [a for a in sample["allowance"] if a["status"] == "estimated" and all(a.get(k) == cap.get(k) for k in ("vendor", "account_id", "environment", "window"))]
                    if len(matching) == 1:
                        values.append((sample, matching[0]["value"]))
            predictions.append(_prediction(index, "phase_forecast_allowance", values, cohort, window=cap["window"], capacity_id=cap.get("observation_id"), reason=reason))
    n = sum(p["status"] == "estimated" for p in predictions)
    forecast = {"interface": "telemetry-phase-forecast-v1", "estimator": ESTIMATOR, "created_at": _iso(now), "basis_run_id": gen.get("run_id"),
        "basis_finished_at": _iso(usage.parse_timestamp(gen.get("finished_at"))), "request": request, "includes": includes(request),
        "status": "estimated" if n == len(predictions) else "partial" if n else "unavailable", "predictions": predictions}
    forecast["forecast_id"] = "fc-" + _digest(forecast)
    return forecast


def _comparison(admission: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    forecast = admission["forecast"]
    linked = [s for s in samples if s["observation"]["forecast_id"] == forecast["forecast_id"]]
    result = {"forecast_id": forecast["forecast_id"], "admitted_at": admission["admitted_at"], "forecast": forecast, "status": "unavailable", "reason": "observation_not_received", "observation_id": None, "observation_revision": None, "actual": None, "errors": []}
    if len(linked) != 1:
        return {**result, "reason": "multiple_phase_observations" if linked else "observation_not_received"}
    sample = linked[0]
    observation = sample["observation"]
    result.update(observation_id=observation["observation_id"], observation_revision=observation["revision"], observation_digest=sample["digest"], actual=sample["actual"], allowance=sample["allowance"])
    if observation["request"] != forecast["request"]:
        return {**result, "reason": "scope_or_execution_changed"}
    if usage.parse_timestamp(observation["started_at"]) < usage.parse_timestamp(admission["admitted_at"]):
        return {**result, "reason": "phase_started_before_admission"}
    if not sample["eligible"]:
        return {**result, "reason": sample["reason"]}
    errors = []
    for prediction in forecast["predictions"]:
        choice = forecast["request"]["choices"][prediction["choice_index"]]
        comp = _choice_component(sample["actual"], choice)
        key = METRICS[prediction["metric_id"]][1]
        value, attribution, reason = comp.get(key) if comp and key else None, "exact", None
        if prediction["metric_id"] == "phase_forecast_api_equivalent_usd" and comp and comp["unpriced_tokens"] != 0:
            value, reason = None, "pricing_not_fully_observed"
        if key is None:
            matching = [a for a in sample["allowance"] if a.get("window") == prediction["window"] and a.get("vendor") == choice["vendor"] and a.get("account_id") == choice["account_id"]]
            a = matching[0] if len(matching) == 1 else {}
            value, attribution, reason = a.get("value"), "estimated", a.get("reason") or "allowance_not_observed"
        valid = value is not None and prediction["estimate"] is not None
        error = round(value - prediction["estimate"], 6) if valid else None
        errors.append({"choice_index": prediction["choice_index"], "metric_id": prediction["metric_id"], "unit": prediction["unit"], "window": prediction["window"], "expected": prediction["estimate"], "actual": value,
            "attribution": attribution, "signed_error": error, "absolute_error": abs(error) if error is not None else None, "status": "comparable" if valid else "unavailable", "reason": None if valid else reason or prediction["reason"] or "actual_not_observed"})
    return {**result, "status": "comparable" if any(e["status"] == "comparable" for e in errors) else "unavailable", "reason": None, "errors": errors}


def _calibration(comparisons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = defaultdict(list)
    for comparison in comparisons:
        for error in comparison["errors"]:
            if error["status"] == "comparable":
                choice = comparison["forecast"]["request"]["choices"][error["choice_index"]]
                request = comparison["forecast"]["request"]
                context = {k: request[k] for k in ("phase", "scope_kind", "workflow_id", "feature_count")}
                key = error["metric_id"], _json(choice), error["window"], error["unit"], error["attribution"], _json(context)
                buckets[key].append(error)
    return [{"metric_id": k[0], "choice": json.loads(k[1]), "window": k[2], "unit": k[3], "attribution": k[4], "context": json.loads(k[5]), "sample_count": len(v),
        "mean_signed_error": round(statistics.mean(e["signed_error"] for e in v), 6), "mean_absolute_error": round(statistics.mean(e["absolute_error"] for e in v), 6)} for k, v in sorted(buckets.items(), key=lambda item: str(item[0]))]


def view(connection: sqlite3.Connection | None, scope: dict[str, Any], *, capacity: list[dict[str, Any]], generation: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    base = {"contract": CONTRACT, "estimator": ESTIMATOR, "status": "available", "forecast": None, "request_status": "not-requested", "request_reason": None,
        "comparisons": [], "calibration": [], "earlier_spent": None, "coverage": {"observations": 0, "eligible_samples": 0, "excluded": {}},
        "basis": "completed phase unions; empirical ranges are not confidence intervals; account-wide allowance movement remains estimated"}
    request = None
    if "forecast" in scope:
        try:
            request = request_metadata(scope["forecast"])
            base["request_status"] = "valid"
        except (MetadataError, TypeError, ValueError):
            base["request_status"], base["request_reason"] = "invalid", "forecast_request_invalid"
    if connection is None or not _has_tables(connection):
        return {**base, "status": "unavailable", "request_reason": base["request_reason"] or "forecast_store_unavailable"}
    admissions, observations, rejected = _events(connection)
    latest = connection.execute("SELECT status FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if latest is not None and latest[0] != "success":
        return {**base, "status": "unavailable", "request_reason": "collection_generation_incomplete",
            "comparisons": [{"forecast_id": key, "forecast": value["forecast"], "status": "unavailable", "reason": "collection_generation_incomplete", "errors": []} for key, value in admissions.items() if key in (scope.get("forecast_comparisons") or [])]}
    samples, excluded = _samples(connection, observations, now)
    for sample in samples:
        admission = admissions.get(sample["observation"]["forecast_id"])
        if admission and sample["eligible"] and sample["observation"]["request"] != admission["forecast"]["request"]:
            sample["eligible"], sample["reason"] = False, "scope_or_execution_changed"
            excluded["scope_or_execution_changed"] += 1
    excluded.update(rejected)
    by_forecast = defaultdict(list)
    for sample in samples:
        by_forecast[sample["observation"]["forecast_id"]].append(sample)
    comparisons = [_comparison(admissions[key], by_forecast[key]) for key in sorted(admissions)]
    base["calibration"] = _calibration(comparisons)
    base["coverage"] = {"observations": len(observations), "eligible_samples": sum(s["eligible"] for s in samples), "admitted_forecasts": len(admissions), "excluded": dict(sorted(excluded.items())),
        "observed_from": min((o["started_at"] for o in observations), default=None), "observed_to": max((o["finished_at"] for o in observations), default=None)}
    if request is not None:
        base["forecast"] = _forecast(request, samples, capacity, generation, now)
    wanted = scope.get("forecast_comparisons")
    if isinstance(wanted, list):
        wanted = {v for v in wanted[:MAX_COMPARISONS] if isinstance(v, str) and re.fullmatch(r"fc-[0-9a-f]{64}", v)}
        base["comparisons"] = [c for c in comparisons if c["forecast_id"] in wanted]
        base["comparisons"].extend({"forecast_id": key, "status": "unavailable", "reason": "forecast_not_admitted", "errors": []} for key in sorted(wanted - set(admissions)))
    prior = scope.get("forecast_prior_outcomes")
    if isinstance(prior, list):
        prior = sorted({v for v in prior[:200] if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", v)})
        rows, _ = outcome_quality._receipts(connection)
        phases = [{r.get("phase") for r in rows.get(oid, []) if r.get("phase")} for oid in prior]
        if not prior or any(not p or not p <= {"research", "context", "definition", "planning"} for p in phases):
            base["earlier_spent"] = {"status": "unavailable", "reason": "earlier_phase_not_established"}
        else:
            base["earlier_spent"] = outcome_quality.phase_usage_union(connection, prior)
            base["earlier_spent"]["basis"] = "caller-selected recorded research/planning; excluded from the upcoming forecast"
    return base


def consumer_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Preserve old scope fields; new raw descriptor extras must not be echoed."""
    clean = {k: v for k, v in scope.items() if k not in ("forecast", "forecast_comparisons", "forecast_prior_outcomes")}
    if "forecast" in scope:
        try:
            clean["forecast"] = request_metadata(scope["forecast"])
        except (MetadataError, TypeError, ValueError):
            clean["forecast"] = None
    for key, pattern, limit in (("forecast_comparisons", r"fc-[0-9a-f]{64}", 100), ("forecast_prior_outcomes", r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", 200)):
        if key in scope:
            clean[key] = [v for v in scope[key][:limit] if isinstance(v, str) and re.fullmatch(pattern, v)] if isinstance(scope[key], list) else []
    return clean
