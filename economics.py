"""Attention Economics: metadata reports and restricted, dated operator context.

Public builders accept sanitized machine facts only. Operator prose, cash-entry
identity, receipt/native identity and reporting maps never enter public outputs.
"""
from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import math
import os
import re
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import outcome_quality
from tools import attention

CONTRACT = 'attention-economics-v1'
LEDGER = 'economics-ledger.jsonl'
MODES = attention.MODES
GOAL_METRICS = {'delivered', 'human_accepted', 'verified_delivered'}
RESULT_FIELDS = ('outcomes', 'delivered', 'reviewed', 'human_accepted', 'needs_changes', 'delivered_unreviewed',
                 'verified_delivered', 'verification_observed', 'checks_passed', 'checks_failed',
                 'attempts', 'repairs', 'human_interventions', 'refinement_observed', 'refinement_complete',
                 'receipt_code_revisions', 'elapsed_seconds', 'closed_waiting_seconds', 'open_waits')


def iso(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _date(value):
    return dt.date.fromisoformat(value)


def _meta(connection, key, default):
    row = connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def retain_configuration(connection, registry, config):
    """Retain only explicit mappings; no private receipt ID is made public."""
    links = (config.get('economics') or {}).get('project_links') or {}
    valid = {k: v for k, v in links.items() if isinstance(k, str) and v in registry['public']}
    with connection:
        connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('economics_project_links', json.dumps(valid)))
        connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('economics_projects', json.dumps(
            {code: row.get('public_label') or code for code, row in registry['public'].items()})))


def outcome_fields(connection):
    """Additive public fields, using the existing receipt/native-session union."""
    by_outcome, owners = outcome_quality._receipts(connection)
    items = outcome_quality._outcome_items(connection, by_outcome, owners)
    registered = _meta(connection, 'economics_projects', {})
    registered.update({r['project_code']: r['public_label'] or r['project_code']
                       for r in connection.execute('SELECT project_code,public_label FROM projects WHERE registered=1')})
    links = _meta(connection, 'economics_project_links', {})
    labels = {label: code for code, label in registered.items()}
    has_code = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_observations'").fetchone()
    fields = {}
    for item in items:
        rows = by_outcome[item['outcome_id']]
        explicit = {links.get(r.get('project_id')) or labels.get(r.get('project_id'))
                    or (r.get('project_id') if r.get('project_id') in registered else None) for r in rows}
        explicit.discard(None)
        pairs = {(r['vendor'], r['native_session_id']) for r in rows if r.get('vendor') and r.get('native_session_id')}
        revisions = {str(r['commit']).lower() for r in rows if isinstance(r.get('commit'), str)
                     and re.fullmatch(r'[0-9a-fA-F]{40,64}', r['commit'])}
        revision_projects = {r['project_id'] for revision in revisions if has_code
                             for r in connection.execute('SELECT project_id FROM code_observations WHERE native_revision=?', (revision,))
                             if r['project_id'] in registered}
        native = set()
        unknown = False
        for vendor, sid in pairs:
            session = connection.execute('SELECT project_code FROM sessions WHERE vendor=? AND session_id=?', (vendor, sid)).fetchone()
            if session and session['project_code'] in registered:
                native.add(session['project_code'])
            else:
                unknown = True
        candidates = explicit | native | revision_projects
        if len(candidates) > 1:
            project, strength = None, 'shared'
        elif len(explicit | revision_projects) == 1:
            project, strength = next(iter(explicit | revision_projects)), 'exact'
        elif len(native) == 1 and not unknown:
            project, strength = next(iter(native)), 'correlated'
        else:
            project, strength = None, 'unattributed'
        attempts = {r['attempt_id'] for r in rows if r.get('attempt_id')}
        bindings = {r['event_id'] for r in rows if r['kind'] == 'session.bound'}
        receipt_revisions = {str(r['commit']).lower() for r in rows if isinstance(r.get('commit'), str)
                            and re.fullmatch(r'[0-9a-fA-F]{7,64}', r['commit'])}
        fields[item['outcome_id']] = {
            'project_id': project, 'project_attribution': strength,
            'delivered': item['delivered'], 'review_verdict': item['verdict'], 'acceptance_basis': item['acceptance_basis'],
            'attempts': len(attempts) if attempts else len(bindings) if bindings else 0 if item['measurement_complete'] else None,
            'attempt_basis': 'explicit_attempt_ids' if attempts else 'session_bindings' if bindings else 'complete_refinement_capture' if item['measurement_complete'] else 'not_observed',
            'repairs': item['repairs'], 'human_interventions': item['human_interventions'],
            'measurement_since': item['measurement_since'], 'measurement_complete': item['measurement_complete'],
            'verification_observed': any(r['kind'] == 'check.result' for r in rows),
            'receipt_code_revisions': len(receipt_revisions) if receipt_revisions else None,
            'usage_attribution': item['usage_attribution'], 'api_equivalent_cost_usd': item['api_equivalent_cost_usd'],
            'unpriced_tokens': item['unpriced_tokens'], 'elapsed_seconds': item['elapsed_seconds'],
            'closed_waiting_seconds': item['closed_waiting_seconds'], 'open_waits': item['open_waits'],
        }
    return fields


def _sum_known(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return sum(values) if values else None


def ensure_evidence_projects(connection, registry, snapshot):
    """Keep registered joins valid even when only code, receipts or timers exist.

    Provider bounds stay null and provider counters stay zero; code/timer dates
    must never masquerade as provider observation timestamps.
    """
    codes = {r[0] for r in connection.execute('SELECT DISTINCT project_id FROM code_observations')}
    codes |= {r['project_id'] for r in outcome_fields(connection).values() if r['project_id'] is not None}
    codes |= {r['project_id'] for r in snapshot.get('metrics', {}).get('attention', {}).get('days', [])}
    with connection:
        for code in codes & set(registry['public']):
            row = registry['public'][code]
            connection.execute('INSERT OR IGNORE INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (code, code, row.get('public_label'), row.get('category', 'registered'), None, None,
                                1, None, None, None, None, None, None, 0, 0, 0.0, 0))


def results(rows, observed):
    """Receipt counts with explicit denominators and unknown refinement capture."""
    if not observed:
        return dict.fromkeys(RESULT_FIELDS)
    verdict = lambda r: r.get('review_verdict')
    return {
        'outcomes': len(rows), 'delivered': sum(bool(r.get('delivered')) for r in rows),
        'reviewed': sum(verdict(r) in ('accepted', 'needs-changes') for r in rows),
        'human_accepted': sum(bool(r.get('delivered')) and verdict(r) == 'accepted' and r.get('acceptance_basis') == 'human' for r in rows),
        'needs_changes': sum(verdict(r) == 'needs-changes' for r in rows),
        'delivered_unreviewed': sum(bool(r.get('delivered')) and verdict(r) is None for r in rows),
        'verified_delivered': sum(bool(r.get('delivered')) and r.get('checks_passed', 0) > 0 and r.get('checks_failed', 0) == 0 for r in rows),
        'verification_observed': sum(bool(r.get('verification_observed')) for r in rows),
        'checks_passed': _sum_known([r for r in rows if r.get('verification_observed')], 'checks_passed'),
        'checks_failed': _sum_known([r for r in rows if r.get('verification_observed')], 'checks_failed'),
        'attempts': _sum_known(rows, 'attempts'), 'repairs': _sum_known(rows, 'repairs'),
        'human_interventions': _sum_known(rows, 'human_interventions'),
        'refinement_observed': sum(r.get('measurement_since') is not None for r in rows),
        'refinement_complete': sum(bool(r.get('measurement_complete')) for r in rows),
        'receipt_code_revisions': _sum_known(rows, 'receipt_code_revisions'),
        'elapsed_seconds': _sum_known(rows, 'elapsed_seconds'),
        'closed_waiting_seconds': _sum_known(rows, 'closed_waiting_seconds'), 'open_waits': _sum_known(rows, 'open_waits'),
    }


def _bounds(snapshot, key):
    end = _date(snapshot['collection']['date']) - dt.timedelta(days=1)
    facts = snapshot.get('metrics', {}).get('economics', {})
    starts = [r['date'] for r in snapshot.get('metrics', {}).get('observatory', {}).get('daily', [])]
    starts += [r['first_at'][:10] for r in facts.get('outcomes', []) if r.get('first_at')]
    starts += [r['date'] for r in facts.get('code_changes', [])]
    starts += [r['date'] for r in snapshot.get('metrics', {}).get('attention', {}).get('days', [])]
    start = min(starts, default=end.isoformat()) if key == 'all' else (end - dt.timedelta(days=int(key)-1)).isoformat()
    return {'from': min(start, end.isoformat()), 'to': end.isoformat(), 'closed_utc': True}


def _coverage(snapshot, bounds):
    metrics = snapshot.get('metrics', {})
    facts = metrics.get('economics', {})
    daily = metrics.get('observatory', {}).get('daily', [])
    outcomes = facts.get('outcomes', [])
    code = facts.get('code_coverage', {})
    timer = metrics.get('attention', {})
    roots = metrics.get('observatory', {}).get('source_roots', [])
    usage_from = min((r['date'] for r in daily), default=None)
    outcome_from = min((r['first_at'][:10] for r in outcomes if r.get('first_at')), default=None)
    return {
        'usage': {'from': usage_from, 'to': bounds['to'] if usage_from else None,
                  'status': 'partial' if any(r.get('status') not in ('ok', 'disabled') for r in roots) else 'observed' if usage_from else 'not-observed',
                  'note': 'Deduplicated configured provider usage; unpriced tokens stay separate.'},
        'attention': {**timer.get('coverage', {'from': None, 'to': None}), 'status': timer.get('status', 'not-observed'),
                      'note': 'Recorded timer intervals only; unrecorded human attention is unknown.'},
        'outcomes': {'from': outcome_from, 'to': bounds['to'] if outcome_from else None,
                     'status': facts.get('outcome_status', 'observed' if outcome_from else 'not-observed'),
                     'note': 'Whole outcomes by last receipt; reviews/refinement are incomplete unless explicitly captured from start.'},
        'code': {'from': code.get('from'), 'to': code.get('to'), 'status': code.get('status', 'not-configured'),
                 'sources': code.get('sources', 0), 'states': code.get('states', {}), 'reasons': code.get('reasons', []),
                 'note': 'Configured first-parent Git history; text shortstat, no quality or productivity inference.'},
    }


def _aggregate(snapshot, bounds, coverage, include_projects=()):
    metrics, facts = snapshot.get('metrics', {}), snapshot.get('metrics', {}).get('economics', {})
    projects = metrics.get('observatory', {}).get('projects', [])
    public_to_code = {r['project_id']: r.get('project_code', r['project_id']) for r in projects}
    labels = {r.get('project_code', r['project_id']): r['project_id'] for r in projects}
    selected = lambda day: day and bounds['from'] <= day <= bounds['to']
    usage_rows = [r for r in metrics.get('observatory', {}).get('daily', []) if selected(r['date'])]
    attention_rows = [r for r in metrics.get('attention', {}).get('days', []) if selected(r['date'])]
    outcome_rows = [r for r in facts.get('outcomes', []) if selected((r.get('last_at') or '')[:10])]
    code_rows = [r for r in facts.get('code_changes', []) if selected(r['date'])]
    outcome_observed = bool(coverage['outcomes'].get('from') and coverage['outcomes']['from'] <= bounds['to'])
    code_observed = coverage['code']['status'] in ('current', 'partial')
    owned = lambda code: code if code in labels and code not in ('ad-hoc', 'remote') else 'Shared/Unassigned'
    parts = defaultdict(lambda: {'usage': [], 'attention': [], 'outcomes': [], 'code': []})
    for row in usage_rows:
        parts[owned(public_to_code.get(row['project_id']))]['usage'].append(row)
    for row in attention_rows:
        parts[owned(row['project_id'])]['attention'].append(row)
    for row in outcome_rows:
        parts[owned(row.get('project_id'))]['outcomes'].append(row)
    for row in code_rows:
        parts[owned(row.get('project_id'))]['code'].append(row)
    for code in include_projects:
        if code in labels or code == 'Shared/Unassigned':
            parts[code]

    def measure(part, project=None):
        timed = part['attention']
        seconds = sum(r['attention_seconds'] for r in timed) if timed else None
        usage_known = bool(part['usage'])
        code = part['code']
        project_code_coverage = facts.get('code_coverage', {}).get('by_project', {}).get(project, {})
        code_known = bool(code) or (code_observed if project is None else project_code_coverage.get('status') == 'current'
                                   and project_code_coverage.get('from') and project_code_coverage['from'] <= bounds['from'])
        result_known = outcome_observed if project is None or project == 'Shared/Unassigned' else any(
            r.get('project_id') == project and r.get('first_at', '')[:10] <= bounds['to'] for r in facts.get('outcomes', []))
        return {
            'recorded_attention_seconds': seconds,
            'recorded_attention_hours': round(seconds / 3600, 6) if seconds is not None else None,
            'mode_seconds': {m: sum(r['mode_seconds'][m] for r in timed) if timed else None for m in MODES},
            'api_equivalent_cost_usd': round(sum(r.get('cost_usd', r.get('api_equivalent_cost_usd', 0)) for r in part['usage']), 6) if usage_known else None,
            'unpriced_tokens': sum(r.get('unpriced_tokens', 0) for r in part['usage']) if usage_known else None,
            'results': results(part['outcomes'], result_known),
            'attribution': dict(Counter(r.get('project_attribution', 'unattributed') for r in part['outcomes'])),
            'code': {'revisions': len(code) if code_known else None,
                     'files_changed': _sum_known(code, 'files_changed') if code else 0 if code_known else None,
                     'insertions': _sum_known(code, 'insertions') if code else 0 if code_known else None,
                     'deletions': _sum_known(code, 'deletions') if code else 0 if code_known else None,
                     'stats_observed': sum(r.get('files_changed') is not None for r in code)},
        }
    rows = [{'project_code': code, 'project_id': labels.get(code, code), **measure(part, code)} for code, part in parts.items()]
    total = measure({'usage': usage_rows, 'attention': attention_rows, 'outcomes': outcome_rows, 'code': code_rows})
    return rows, total


def _deltas(current, previous, coverage, prior_bounds):
    paths = {'recorded_attention_hours': ('attention', None), 'api_equivalent_cost_usd': ('usage', None),
             'delivered': ('outcomes', 'results'), 'human_accepted': ('outcomes', 'results'),
             'verified_delivered': ('outcomes', 'results'), 'revisions': ('code', 'code')}
    values, observed_values, reasons = {}, {}, {}
    for key, (family, nested) in paths.items():
        source = coverage[family]
        a, b = (current.get(nested, {}) if nested else current).get(key), (previous.get(nested, {}) if nested else previous).get(key)
        complete = source.get('from') and source['from'] <= prior_bounds['from'] and source.get('to') and source['to'] >= prior_bounds['to']
        healthy = source['status'] in ('observed', 'available', 'current')
        observed_values[key] = round(a - b, 6) if a is not None and b is not None and complete else None
        values[key] = observed_values[key] if healthy else None
        if values[key] is None:
            reasons[key] = 'incomplete_or_missing_coverage'
    return {'values': values, 'observed_values': observed_values, 'reasons': reasons,
            'qualification': 'Same closed UTC dates and definitions; timer completeness and comparable work scope are not established.'}


def report(snapshot, key):
    """Bounded public report reconciled to the same generation's machine facts."""
    bounds = _bounds(snapshot, key)
    coverage = _coverage(snapshot, bounds)
    rows, total = _aggregate(snapshot, bounds, coverage)
    previous, prior_bounds = None, None
    if key != 'all':
        prior_bounds = {'from': (_date(bounds['from']) - dt.timedelta(days=int(key))).isoformat(),
                        'to': (_date(bounds['from']) - dt.timedelta(days=1)).isoformat(), 'closed_utc': True}
        prior_rows, previous = _aggregate(snapshot, prior_bounds, coverage)
        prior_by_code = {r['project_code']: r for r in prior_rows}
        # A project present only in the prior period stays visible, with unknown
        # attention and zero recorded usage/results only where observed.
        codes = {r['project_code'] for r in rows}
        prior_codes = set(prior_by_code)
        if prior_codes - codes:
            rows, total = _aggregate(snapshot, bounds, coverage, prior_codes)
        for row in rows:
            old = prior_by_code.get(row['project_code'])
            row['previous'] = old
            row['change'] = _deltas(row, old or {}, coverage, prior_bounds)
    rows.sort(key=lambda r: (-(r['api_equivalent_cost_usd'] or 0), r['project_code']))
    shared = next((r for r in rows if r['project_code'] == 'Shared/Unassigned'), None)
    ranked = [r for r in rows if r['project_code'] != 'Shared/Unassigned']
    preview = ranked[:6]
    if ranked[6:]:
        # Reaggregate the tail's actual facts, so nulls, denominators and dollars
        # retain exactly the same definitions as the complete totals.
        codes = {r['project_code'] for r in ranked[6:]}
        tail_snapshot = json.loads(json.dumps(snapshot))
        tm = tail_snapshot['metrics']
        allowed_public = {r['project_id'] for r in tm['observatory'].get('projects', []) if r.get('project_code', r['project_id']) in codes}
        tm['observatory']['daily'] = [r for r in tm['observatory'].get('daily', []) if r['project_id'] in allowed_public]
        tm.setdefault('attention', {})['days'] = [r for r in tm.get('attention', {}).get('days', []) if r['project_id'] in codes]
        tm.setdefault('economics', {})['outcomes'] = [r for r in tm.get('economics', {}).get('outcomes', []) if r.get('project_id') in codes]
        tm['economics']['code_changes'] = [r for r in tm['economics'].get('code_changes', []) if r.get('project_id') in codes]
        _, tail = _aggregate(tail_snapshot, bounds, coverage)
        if prior_bounds:
            _, old_tail = _aggregate(tail_snapshot, prior_bounds, coverage)
            tail.update(previous=old_tail, change=_deltas(tail, old_tail, coverage, prior_bounds))
        preview.append({'project_code': 'other', 'project_id': 'other', 'other_count': len(codes), **tail})
    trend = []
    span = (_date(bounds['to']) - _date(bounds['from'])).days + 1
    step = max(1, math.ceil(span / 48))
    for offset in range(0, span, step):
        start = _date(bounds['from']) + dt.timedelta(days=offset)
        end = min(_date(bounds['to']), start + dt.timedelta(days=step-1))
        _, bucket = _aggregate(snapshot, {'from': start.isoformat(), 'to': end.isoformat()}, coverage)
        trend.append({'from': start.isoformat(), 'to': end.isoformat(), 'attention_hours': bucket['recorded_attention_hours'],
                      'api_equivalent_cost_usd': bucket['api_equivalent_cost_usd'], 'delivered': bucket['results']['delivered'],
                      'human_accepted': bucket['results']['human_accepted'], 'code_revisions': bucket['code']['revisions']})
    return {'contract': CONTRACT, 'period': bounds, 'previous_period': prior_bounds,
            'totals': total, 'previous': previous, 'change': _deltas(total, previous, coverage, prior_bounds) if previous else None,
            'projects': preview, 'project_count': len(ranked), 'shared': shared, 'trend': trend, 'coverage': coverage,
            'subscription': snapshot.get('metrics', {}).get('economics', {}).get('subscriptions', {}).get(key, {'status': 'not-configured', 'estimate_usd': None}),
            'actual_cash': snapshot.get('metrics', {}).get('economics', {}).get('cash', {}).get(key, {'status': 'not-published', 'amount_usd': None}),
            'basis': 'Investment is a union of recorded project usage and timer evidence. Outcomes select whole records by last receipt; costs include ordinary usage and repair, not only successful attempts. Code metadata is independent of outcomes.',
            'worth': {'status': 'requires_explicit_goal_or_assumption', 'assessment': None}}


def subscription_view(project_root, bounds):
    """Dated rate estimates, never historical bills or project allocations."""
    base = {'status': 'not-configured', 'current_monthly_usd': None, 'estimate_usd': None,
            'covered_estimate_usd': None, 'covered_vendor_days': 0, 'missing_vendor_days': None,
            'basis': 'Explicitly dated monthly rate / calendar days in month; estimates are separate from payments.'}
    path = project_root / 'subscriptions.local.json'
    if not path.is_file():
        return base
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        def rates(raw):
            if not isinstance(raw, dict) or not raw:
                raise ValueError('rates_invalid')
            if any(k not in ('anthropic', 'openai', 'cursor') or type(v) not in (int, float) or not math.isfinite(v) or v < 0 for k, v in raw.items()):
                raise ValueError('rates_invalid')
            return raw
        current = rates(value['monthly_usd']) if value.get('monthly_usd') is not None else {}
        schedule = []
        for raw in value.get('periods', []):
            start, end = _date(raw['from']), _date(raw['to']) if raw.get('to') else dt.date.max
            if end < start:
                raise ValueError('subscription_dates_invalid')
            schedule.append((start, end, rates(raw['monthly_usd'])))
        for index, (a, b, row) in enumerate(schedule):
            if any(max(a, c) <= min(b, d) and set(row) & set(other) for c, d, other in schedule[:index]):
                raise ValueError('subscription_dates_overlap')
        vendors = set(current) | {k for _, _, row in schedule for k in row}
        total, covered, missing = 0.0, 0, 0
        day = _date(bounds['from'])
        while day <= _date(bounds['to']):
            for vendor in vendors:
                rate = next((r[vendor] for a, b, r in schedule if a <= day <= b and vendor in r), None)
                if rate is None:
                    missing += 1
                else:
                    total += rate / calendar.monthrange(day.year, day.month)[1]
                    covered += 1
            day += dt.timedelta(days=1)
        return {**base, 'status': 'dated-estimate' if covered and not missing else 'partial-dated-estimate' if covered else 'undated-current-rate',
                'current_monthly_usd': round(sum(current.values()), 2) if current else None,
                'estimate_usd': round(total, 6) if covered and not missing else None,
                'covered_estimate_usd': round(total, 6) if covered else None,
                'covered_vendor_days': covered, 'missing_vendor_days': missing}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {**base, 'status': 'invalid-configuration'}


def load_ledger(state_root):
    """Private bounded records, never copied into a public snapshot."""
    path = state_root / LEDGER
    if not path.exists():
        return [], 'not-recorded'
    records = []
    try:
        if path.is_symlink() or path.stat().st_size > 32_000_000 or path.stat().st_mode & 0o077:
            return [], 'invalid-ledger'
        with path.open('rb') as handle:
            for line in attention._iter_bounded_ledger_lines(handle):
                if line is None:
                    return [], 'invalid-ledger'
                row = json.loads(line)
                # Record validation is shared with the CLI; stored dates and
                # generated identity must be present, not synthesized on read.
                if attention.parse_utc_timestamp(row.get('recorded_at')) is None or not isinstance(row.get('event_id'), str):
                    return [], 'invalid-ledger'
                clean = validate_record(row)
                records.append({**clean, 'recorded_at': iso(attention.parse_utc_timestamp(row['recorded_at'])), 'event_id': row['event_id']})
    except (OSError, ValueError, TypeError, KeyError):
        return [], 'invalid-ledger'
    latest = {}
    for row in records:
        key = row['kind'], row['entry_id']
        if key not in latest or row['revision'] > latest[key]['revision']:
            latest[key] = row
    return records, 'recorded'


def validate_record(raw):
    if not isinstance(raw, dict) or raw.get('kind') not in ('context', 'cash'):
        raise ValueError('economics_kind_invalid')
    common = {'kind', 'entry_id', 'revision', 'project_id', 'provenance', 'effective_at'}
    fields = {'context': {'status', 'goal', 'context', 'goal_metric', 'goal_target'},
              'cash': {'date', 'amount_cents', 'category'}}
    allowed = common | fields[raw['kind']] | {'event_id', 'recorded_at'}
    if set(raw) - allowed or not isinstance(raw.get('entry_id'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', raw['entry_id']):
        raise ValueError('economics_identity_or_fields_invalid')
    if type(raw.get('revision')) is not int or raw['revision'] < 1:
        raise ValueError('economics_revision_invalid')
    if attention.parse_utc_timestamp(raw.get('effective_at')) is None:
        raise ValueError('economics_effective_date_invalid')
    if not isinstance(raw.get('provenance'), str) or not 0 < len(raw['provenance']) <= 500:
        raise ValueError('economics_provenance_required')
    if raw.get('project_id') is not None and (not isinstance(raw['project_id'], str) or not attention.PROJECT_ID_RE.fullmatch(raw['project_id'])):
        raise ValueError('economics_project_invalid')
    if raw['kind'] == 'context':
        if raw.get('project_id') is None or raw.get('status') not in ('planned', 'active', 'paused', 'completed', 'retired', 'unknown'):
            raise ValueError('economics_context_status_invalid')
        if any(k in raw and (not isinstance(raw[k], str) or len(raw[k]) > 1000) for k in ('goal', 'context')):
            raise ValueError('economics_context_text_invalid')
        if raw.get('goal_metric') is not None:
            if raw['goal_metric'] not in GOAL_METRICS or type(raw.get('goal_target')) not in (int, float) or not math.isfinite(raw['goal_target']) or raw['goal_target'] < 0:
                raise ValueError('economics_goal_invalid')
        elif raw.get('goal_target') is not None:
            raise ValueError('economics_goal_metric_required')
    else:
        _date(raw['date'])
        if type(raw.get('amount_cents')) is not int or raw['amount_cents'] < 0 or raw.get('category') not in ('subscription', 'api', 'other'):
            raise ValueError('economics_cash_invalid')
    clean = {k: v for k, v in raw.items() if k in common | fields[raw['kind']]}
    clean['effective_at'] = iso(attention.parse_utc_timestamp(raw['effective_at']))
    return clean


def append_record(project_root, state_root, raw, now):
    clean = validate_record(raw)
    if clean.get('project_id') is not None and clean['project_id'] not in attention.load_public_project_ids(project_root):
        raise ValueError('economics_project_unregistered')
    with attention.attention_lock(state_root):
        records, status = load_ledger(state_root)
        if status == 'invalid-ledger':
            raise ValueError(status)
        prior = [r for r in records if (r['kind'], r['entry_id']) == (clean['kind'], clean['entry_id'])]
        for row in prior:
            if row['revision'] == clean['revision']:
                if validate_record(row) == clean:
                    return 'already-recorded'
                raise ValueError('economics_revision_conflict')
        if clean['revision'] != max((r['revision'] for r in prior), default=0) + 1:
            raise ValueError('economics_revision_sequence_invalid')
        row = {**clean, 'recorded_at': iso(now), 'event_id': str(uuid.uuid4())}
        payload = (json.dumps(row, sort_keys=True, ensure_ascii=True) + '\n').encode('utf-8')
        if len(payload) > attention.MAX_LEDGER_LINE_BYTES:
            raise ValueError('economics_record_oversized')
        fd = os.open(state_root / LEDGER, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            if os.write(fd, payload) != len(payload):
                raise ValueError('economics_append_incomplete')
            os.fsync(fd)
        finally:
            os.close(fd)
        return 'recorded'


def local_context(state_root, bounds, as_of, rows):
    records, status = load_ledger(state_root)
    eligible = [r for r in records if r['recorded_at'] <= as_of and r['effective_at'][:10] <= bounds['to']]
    latest = {}
    for r in eligible:
        key = r['kind'], r['entry_id']
        if key not in latest or r['revision'] > latest[key]['revision']:
            latest[key] = r
    contexts = {}
    for r in latest.values():
        if r['kind'] == 'context' and (r['project_id'] not in contexts or (r['effective_at'], r['recorded_at']) > (contexts[r['project_id']]['effective_at'], contexts[r['project_id']]['recorded_at'])):
            contexts[r['project_id']] = r
    measured = {r['project_code']: r for r in rows}
    for pid, context in contexts.items():
        metric = context.get('goal_metric')
        actual = (measured.get(pid) or {}).get('results', {}).get(metric)
        context = dict(context)
        context['goal_assessment'] = {'metric': metric, 'target': context.get('goal_target'), 'observed': actual,
                                      'met': actual >= context['goal_target'] if metric and actual is not None else None,
                                      'basis': 'Explicit operator target for this reporting period; no general worth assessment.'}
        contexts[pid] = context
    cash = [r for r in latest.values() if r['kind'] == 'cash' and bounds['from'] <= r['date'] <= bounds['to']]
    by_project = defaultdict(int)
    by_category = defaultdict(int)
    for r in cash:
        by_project[r.get('project_id') or 'Shared/Unassigned'] += r['amount_cents']
        by_category[r['category']] += r['amount_cents']
    return {'ledger_status': status, 'contexts': contexts,
            'actual_cash': {'status': 'recorded' if cash else 'not-recorded', 'amount_usd': sum(r['amount_cents'] for r in cash) / 100 if cash else None,
                            'entries': len(cash), 'by_project_usd': {k: v / 100 for k, v in by_project.items()},
                            'by_category_usd': {k: v / 100 for k, v in by_category.items()},
                            'basis': 'Latest cash revisions booked in UTC period; incomplete entry ledger, independent of API-equivalent USD and subscription estimates.'}}


def collect_facts(project_root, state_root, config, snapshot):
    """Read the just-collected generation; retain only public metadata in it."""
    import sqlite3
    import observatory
    import outcomes
    import code_evidence
    path = state_root / observatory.STORE_NAME
    facts = {'outcomes': [], 'code_changes': [], 'outcome_status': 'not-observed',
             'code_coverage': {'status': 'not-configured', 'sources': 0}, 'subscriptions': {}}
    if path.is_file() and snapshot.get('metrics', {}).get('observatory', {}).get('status') != 'disabled':
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN')
            facts['outcomes'] = outcomes.public_rows(con)[0]
            facts['code_changes'] = code_evidence.public_rows(con)
            facts['code_coverage'] = code_evidence.coverage(con)
            roots = list(con.execute('SELECT status FROM receipt_roots'))
            facts['outcome_status'] = 'partial' if any(r[0] != 'ok' for r in roots) else 'observed' if facts['outcomes'] else 'not-observed'
        finally:
            con.close()
    snapshot.setdefault('metrics', {})['economics'] = facts
    for key in ('7', '30', '90', 'all'):
        bounds = _bounds(snapshot, key)
        facts['subscriptions'][key] = subscription_view(project_root, bounds)
        if key != 'all':
            prior = {'from': (_date(bounds['from']) - dt.timedelta(days=int(key))).isoformat(),
                     'to': (_date(bounds['from']) - dt.timedelta(days=1)).isoformat()}
            previous = subscription_view(project_root, prior)
            current = facts['subscriptions'][key]
            current['previous'] = previous
            current['change_usd'] = round(current['estimate_usd'] - previous['estimate_usd'], 6) if current['estimate_usd'] is not None and previous['estimate_usd'] is not None else None
        if (config.get('economics') or {}).get('publish_cash_aggregates') is True:
            cash = local_context(state_root, bounds, snapshot['generated_at'], [])['actual_cash']
            facts.setdefault('cash', {})[key] = {k: cash[k] for k in ('status', 'amount_usd', 'entries', 'basis')}
    return facts


def consumer_view(connection, project_root, state_root, scope, now, gen, accounting, registry, intervals):
    """Additive read-only local capability; the reporting map retains authority."""
    import observatory
    import outcomes
    import code_evidence
    if connection is None:
        return {'contract': CONTRACT, 'status': 'not-configured', 'report': None, 'context': None}
    key = str(scope.get('days', 30))
    if key not in ('7', '30', '90', 'all'):
        return {'contract': CONTRACT, 'status': 'unsupported-window', 'report': None, 'context': None}
    snapshot = {'generated_at': iso(now), 'collection': {'date': now.astimezone(dt.timezone.utc).date().isoformat()},
                'metrics': {'observatory': observatory.public_summary(connection), 'economics': {
                    'outcomes': outcomes.public_rows(connection)[0], 'code_changes': code_evidence.public_rows(connection),
                    'code_coverage': code_evidence.coverage(connection)}}}
    import portfolio

    buckets = portfolio._collect_buckets(connection, None, snapshot['collection']['date'], lambda r: [(r['day_utc'], r['project_id'])])
    labels = {r['project_id']: r['public_label'] or r['project_code'] for r in connection.execute('SELECT project_id,public_label,project_code FROM projects')}
    snapshot['metrics']['observatory']['daily'] = [
        {'date': day, 'project_id': labels.get(pid, pid), 'cost_usd': portfolio._metrics(bucket)['api_equivalent_cost_usd'],
         'unpriced_tokens': portfolio._metrics(bucket)['unpriced_tokens']}
        for (day, pid), bucket in buckets.items()]
    try:
        parsed = attention.parse_ledger(state_root, attention.load_public_project_map(project_root), now=now)
        deferred = attention.active_deferred_dates(state_root, now=now)
        days = [r for r in attention.aggregate_attention_days(parsed.intervals, deferred_dates=deferred) if r['date'] < snapshot['collection']['date']]
        snapshot['metrics']['attention'] = {'days': days, 'status': 'available' if days else 'no_records',
                    'coverage': {'from': min((r['date'] for r in days), default=None),
                                 'to': (now.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat() if days else None}}
    except attention.AttentionError:
        snapshot['metrics']['attention'] = {'days': [], 'status': 'error', 'coverage': {'from': None, 'to': None}}
    bounds = _bounds(snapshot, key)
    snapshot['metrics']['economics']['subscriptions'] = {key: subscription_view(project_root, bounds)}
    view = report(snapshot, key)
    all_projects, _ = _aggregate(snapshot, bounds, view['coverage'])
    local = local_context(state_root, bounds, iso(now), all_projects)
    view['actual_cash'] = local['actual_cash']
    if view['previous_period']:
        prior_bounds = view['previous_period']
        prior_projects, _ = _aggregate(snapshot, prior_bounds, view['coverage'])
        local['previous'] = local_context(state_root, prior_bounds, iso(now), prior_projects)
        view['actual_cash_previous'] = local['previous']['actual_cash']
        previous_subscription = subscription_view(project_root, prior_bounds)
        view['subscription']['previous'] = previous_subscription
        current_usd, previous_usd = view['subscription']['estimate_usd'], previous_subscription['estimate_usd']
        view['subscription']['change_usd'] = round(current_usd - previous_usd, 6) if current_usd is not None and previous_usd is not None else None
    # Reuse the exact existing workspace union, allocation and remainder logic.
    # This economics period ends yesterday; other consumer accounting remains
    # its earlier inclusive-current-day view.
    workspace = outcome_quality.accounting_view(connection, reporting=scope.get('reporting'), days=key if key == 'all' else int(key),
                now=now-dt.timedelta(days=1), registry=registry, attention=intervals,
                detail=scope.get('accounting_detail')) if scope.get('reporting') else None
    return {'contract': CONTRACT, 'status': 'observed', 'generation': gen, 'report': view,
            'projects': all_projects, 'context': local, 'workspace': workspace,
            'current_context': local_context(state_root, {'from': bounds['from'], 'to': snapshot['collection']['date']}, iso(now), all_projects)['contexts'],
            'units': {'recorded_attention_seconds': 'seconds of recorded human attention', 'api_equivalent_cost_usd': 'API-equivalent USD',
                      'actual_cash': 'recorded USD', 'subscription': 'estimated USD', 'code': 'revisions / files / text lines',
                      'elapsed_seconds': 'receipt span seconds, not human attention'},
            'uncertainty': 'Configured evidence is incomplete; private goals and explicit assumptions explain, never change, measured values.'}


def archive_reports(project_root, state_root, snapshot):
    """One immutable first report per closed UTC end-date, plus a current view."""
    views = {}
    for key in ('7', '30', '90', 'all'):
        view = report(snapshot, key)
        rows, _ = _aggregate(snapshot, view['period'], view['coverage'])
        views[key] = {'report': view, 'projects': rows,
                      'context': local_context(state_root, view['period'], snapshot['generated_at'], rows),
                      'current_context': local_context(state_root, {'from': view['period']['from'], 'to': snapshot['collection']['date']}, snapshot['generated_at'], rows)['contexts']}
    value = {'contract': CONTRACT, 'as_of': snapshot['generated_at'], 'reports': views}
    root = state_root / 'economics-reports'
    attention._private_directory(root)
    attention._atomic_private_json(root / 'latest.json', value)
    fd, temporary_html = tempfile.mkstemp(prefix='.report-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(local_report_html(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_html, 0o600)
        os.replace(temporary_html, root / 'latest.html')
    finally:
        if os.path.exists(temporary_html):
            os.unlink(temporary_html)
    path = root / (views['30']['report']['period']['to'] + '.json')
    payload = (json.dumps(value, sort_keys=True, ensure_ascii=True) + '\n').encode('utf-8')
    if path.exists():
        return
    fd, temporary = tempfile.mkstemp(prefix='.archive-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        attention._fsync_directory(root)
    finally:
        os.unlink(temporary)


def local_report_html(value):
    """Offline restricted report: private context is escaped, never executed."""
    esc = lambda v: html.escape(str(v), quote=True)
    number = lambda v: 'unknown' if v is None else f'{v:,.2f}'
    sections = []
    for key, item in value['reports'].items():
        view, context = item['report'], item['context']
        totals, bounds = view['totals'], view['period']
        results = totals['results']
        cash = context['actual_cash']
        rows = []
        for row in item['projects']:
            facts = row['results']
            cells = [row['project_id'], number(row['recorded_attention_hours']), number(row['api_equivalent_cost_usd']),
                     number(cash['by_project_usd'].get(row['project_code'])), number(facts['delivered']),
                     number(facts['human_accepted']), number(facts['verified_delivered']), number(row['code']['revisions'])]
            rows.append('<tr>' + ''.join(f'<td>{esc(c)}</td>' for c in cells) + '</tr>')
        contexts = []
        for pid, record in sorted(item['current_context'].items()):
            historical = context['contexts'].get(pid)
            note = 'Effective during the displayed period' if historical and historical['event_id'] == record['event_id'] else 'Current context; not applied to this earlier period'
            assessment = (historical or {}).get('goal_assessment') or {}
            earlier_goal = f'<p><strong>Period-effective goal:</strong> {esc(historical.get("goal") or "No operator goal recorded")}; revision {historical["revision"]}.</p>' if historical and historical['event_id'] != record['event_id'] else ''
            contexts.append(f'<details><summary>{esc(pid)} · status {esc(record["status"])}</summary>'
                            f'<p>{esc(note)}. Effective {esc(record["effective_at"])}; recorded {esc(record["recorded_at"])}; revision {record["revision"]}.</p>'
                            f'<p><strong>Goal:</strong> {esc(record.get("goal") or "No operator goal recorded")}</p>'
                            f'<p>{esc(record.get("context") or "")}</p><p><strong>Provenance:</strong> {esc(record["provenance"])}</p>'
                            + earlier_goal + f'<p>Period-effective goal metric: {esc(assessment.get("metric") or "none")}; target {number(assessment.get("target"))}; '
                            f'observed {number(assessment.get("observed"))}; met {esc(assessment.get("met") if assessment.get("met") is not None else "unknown")}.</p></details>')
        coverage = ' · '.join(f'{family}: {v["status"]}, {v.get("from") or "unknown start"} → {v.get("to") or "unknown end"}' for family, v in view['coverage'].items())
        changes = []
        if view['change']:
            for field, observed in view['change']['observed_values'].items():
                complete = view['change']['values'][field]
                changes.append(f'{field}: {number(observed)}' + (' (retained observations; partial coverage)' if complete is None and observed is not None else ''))
        modes = ' · '.join(f'{mode}: {number(totals["mode_seconds"][mode] / 3600 if totals["mode_seconds"][mode] is not None else None)} h' for mode in MODES)
        sections.append(f'<details class="window" id="window-{key}" {"open" if key == "30" else ""}><summary>{esc(key)} · {esc(bounds["from"])} → {esc(bounds["to"])} closed UTC</summary>'
                        f'<div class="figures"><p><strong>{number(totals["recorded_attention_hours"])}</strong> recorded human hours</p>'
                        f'<p><strong>{number(totals["api_equivalent_cost_usd"])}</strong> API-equivalent USD</p>'
                        f'<p><strong>{number(cash["amount_usd"])}</strong> actual recorded USD</p>'
                        f'<p><strong>{number(results["delivered"])}</strong> delivered; {number(results["human_accepted"])} human accepted</p></div>'
                        f'<p>Recorded closed-period modes: {esc(modes)}.</p><p>{esc(coverage)}</p><p>Dated subscription estimate: {number(view["subscription"].get("estimate_usd"))} USD; '
                        f'current configured rate {number(view["subscription"].get("current_monthly_usd"))} USD/month. These are separate from cash and API-equivalent cost.</p>'
                        f'<p>{esc(" · ".join(changes) or "All-history has no previous period")}</p>'
                        f'<p>Checks observed for {number(results["verification_observed"])} outcomes; recorded pass/fail {number(results["checks_passed"])} / {number(results["checks_failed"])}. '
                        f'Attempts {number(results["attempts"])}; repairs {number(results["repairs"])}; interventions {number(results["human_interventions"])}. '
                        f'Complete refinement capture {number(results["refinement_complete"])} / {number(results["outcomes"])} outcomes.</p>'
                        '<div role="region" aria-label="Project investment and recorded results" tabindex="0"><table><thead><tr>'
                        '<th>Project</th><th>Recorded hours</th><th>API-equivalent USD</th><th>Actual cash USD</th><th>Delivered</th><th>Human accepted</th><th>Passing checks</th><th>Code revisions</th>'
                        '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
                        '<h2>Dated project context</h2>' + (''.join(contexts) or '<p>No operator context recorded.</p>') + '</details>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Attention Economics · restricted report</title>' \
           '<style>html{color-scheme:dark}body{font:16px/1.5 system-ui;background:#0b1017;color:#edf3fa;max-width:1200px;margin:auto;padding:24px}p{overflow-wrap:anywhere}details{border:1px solid #465166;border-radius:10px;padding:16px;margin:12px 0}summary{cursor:pointer;font-weight:bold}summary:focus-visible,[tabindex]:focus-visible{outline:3px solid #ffd166;outline-offset:4px}.figures{display:flex;flex-wrap:wrap;gap:24px}.figures strong{display:block;font-size:1.5rem}[role=region]{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;text-align:left;border-bottom:1px solid #465166}th{white-space:nowrap}</style>' \
           f'<h1>Attention Economics · restricted report</h1><p>As observed {esc(value["as_of"])}. Private context and spending; keep this file local.</p>' \
           '<p>Investment beside recorded results. Missing attention, cash and capture stay unknown. Timer modes are explicit; elapsed spans, code revisions and text lines do not score productivity or quality. Goals and assumptions explain worth; no hourly value or counterfactual is assumed. Frozen loop history remains a separate cohort.</p>' + ''.join(sections) + '</html>'
