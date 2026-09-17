"""Read-only comparable outcome measurements over the private canonical store.

Receipt metadata owns acceptance and interventions; provider tables own usage.
An exact session join is insufficient to divide a shared session's lifetime cost.
"""
from __future__ import annotations

import json
from collections import defaultdict

import usage
import observatory


def _seconds(start, end):
    a, b = usage.parse_timestamp(start), usage.parse_timestamp(end)
    return max(0, (b-a).total_seconds()) if a and b and b >= a else None


def _waiting(rows):
    opened, intervals = {}, []
    for r in rows:
        key = r.get('question_id') if r['kind'].startswith('question.') else 'trigger'
        if r['kind']=='question.opened' or (r['kind']=='outcome.disposition' and r.get('disposition')=='waiting'):
            opened.setdefault(key, r['at'])
        elif r['kind']=='question.answered' or (r['kind']=='outcome.disposition' and r.get('disposition')=='active'):
            if key in opened:
                a,b=usage.parse_timestamp(opened.pop(key)),usage.parse_timestamp(r['at'])
                if a and b and b >= a:
                    intervals.append((a,b))
    merged=[]
    for a,b in sorted(intervals):
        if merged and a<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:
            merged.append((a,b))
    return (sum((b-a).total_seconds() for a,b in merged) if intervals else None),len(opened)


def _receipts(connection):
    by_outcome=defaultdict(list)
    native_owners=defaultdict(set)
    for row in connection.execute('SELECT record_json FROM outcome_events WHERE outcome_id IS NOT NULL ORDER BY at,ledger_seq,event_id'):
        r=json.loads(row['record_json'])
        oid=r['outcome_id']
        by_outcome[oid].append(r)
        if r.get('native_session_id') and r.get('vendor'):
            native_owners[(r['vendor'],r['native_session_id'])].add(oid)
    return by_outcome, native_owners


def quality_view(connection, *, project_id=None, days=None):
    """One receipt-backed row per outcome, plus compact comparable-class totals.

No scan, provider call, prompt body or source path is returned. The receipt
range bounds elapsed/wait evidence; it is not a human-attention measurement.
"""
    by_outcome, native_owners = _receipts(connection)
    items=_outcome_items(connection, by_outcome, native_owners, project_id=project_id, days=days)
    return _quality(items)


def _outcome_items(connection, by_outcome, native_owners, *, project_id=None, days=None):
    items=[]
    for oid, rows in by_outcome.items():
        project=next((r.get('project_id') for r in rows if r.get('project_id')),None)
        if project_id and project_id!=project:
            continue
        # A days filter selects whole outcomes by last observation, just as the
        # existing consumer selects whole provider sessions by last observation.
        if days:
            import datetime as dt
            last=usage.parse_timestamp(rows[-1]['at'])
            if last and last < dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=int(days)):
                continue
        latest=lambda key: next((r.get(key) for r in reversed(rows) if r.get(key) is not None),None)
        comparable=lambda key: next(iter(values)) if len(values := {r[key] for r in rows if r.get(key) is not None}) == 1 else 'mixed' if values else None
        review=next((r for r in reversed(rows) if r['kind']=='review.recorded'),{})
        tool={r['tool_call_digest']:r.get('tool_status') for r in rows if r['kind']=='tool.observed' and r.get('tool_call_digest')}
        pairs={(r['vendor'],r['native_session_id']) for r in rows if r.get('vendor') and r.get('native_session_id')}
        shared=any(len(native_owners[p])>1 for p in pairs)
        sessions=[]
        if not shared:
            for vendor,sid in pairs:
                s=connection.execute('SELECT vendor,tokens_json,cost_usd,unpriced_tokens FROM sessions WHERE vendor=? AND session_id=?',(vendor,sid)).fetchone()
                if s:
                    sessions.append(s)
        complete=bool(pairs) and not shared and len(sessions)==len(pairs)
        waiting,open_waits=_waiting(rows)
        measurement = next((r for r in rows if r.get('measurement_version') == 2 or r['kind'] in ('review.recorded','feedback.recorded','human.intervention','tool.observed')), None)
        item={'measurement_since':measurement['at'] if measurement else None, 'measurement_complete':bool(measurement and measurement['kind']=='outcome.started'), 'outcome_id':oid,'project_id':project,'outcome_kind':rows[0].get('outcome_kind'),
            'environment':comparable('environment'), 'workflow_identity':latest('workflow_identity'),'policy_revision':comparable('policy_revision'),
            'model':comparable('model'),'effort':comparable('effort'),'first_at':rows[0]['at'],'last_at':rows[-1]['at'],
            'delivered':any(r['kind']=='outcome.disposition' and r.get('disposition')=='satisfied' for r in rows),
            'verdict':review.get('verdict'),'acceptance_basis':review.get('acceptance_basis'),
            'feedback_count':len({r.get('feedback_id') or r['event_id'] for r in rows if r['kind']=='feedback.recorded'}) if measurement else None,
            'repairs':len({r['repair_of'] for r in rows if r.get('repair_of')}) if measurement else None,
            'human_interventions':len({r.get('command_id') or r['event_id'] for r in rows if r['kind']=='human.intervention'}) if measurement else None,
            'tool_calls':len(tool) if tool else None,'tool_failures':sum(s=='failed' for s in tool.values()) if tool and all(s in ('failed','succeeded') for s in tool.values()) else None,
            'elapsed_seconds':_seconds(rows[0]['at'],rows[-1]['at']),'closed_waiting_seconds':waiting,'open_waits':open_waits,
            'usage_attribution':'shared-session' if shared else 'exact-exclusive-session' if complete else 'incomplete',
            'usage_sessions_observed':len(sessions),'usage_sessions_bound':len(pairs),
            'tokens':sum(usage.token_total(s['vendor'], observatory.vendor_classes(s['vendor'], json.loads(s['tokens_json']))) for s in sessions) if complete else None,
            'api_equivalent_cost_usd':round(sum(s['cost_usd'] for s in sessions),6) if complete else None,
            'unpriced_tokens':sum(s['unpriced_tokens'] for s in sessions) if complete else None}
        items.append(item)
    return items


def _quality(items):
    grouped={}
    for i in items:
        key=(i['outcome_kind'],i['workflow_identity'],i['model'],i['policy_revision'],i['environment'])
        g=grouped.setdefault(key,{'outcome_kind':key[0],'workflow_identity':key[1],'model':key[2],'policy_revision':key[3], 'environment':key[4],
            'outcomes':0,'delivered':0,'human_accepted':0,'needs_changes':0,'unclassified_reviews':0,'feedback':None,'repairs':None,'human_interventions':None,'measurement_outcomes_observed':0,
            'tokens':None,'api_equivalent_cost_usd':None,'usage_outcomes_observed':0,'unpriced_tokens':None,'tool_calls':None,'tool_failures':None,'tool_outcomes_observed':0,'elapsed_seconds':0,'closed_waiting_seconds':None,'open_waits':0})
        g['outcomes']+=1;g['delivered']+=int(i['delivered'])
        g['human_accepted']+=int(i['delivered'] and i['verdict']=='accepted' and i['acceptance_basis']=='human')
        g['needs_changes']+=int(i['verdict']=='needs-changes')
        g['unclassified_reviews']+=int(i['acceptance_basis']=='review-recorded' and i['verdict'] is None)
        for target,source in [('elapsed_seconds','elapsed_seconds'),('open_waits','open_waits')]:
            g[target]+=i[source] or 0
        for target, source in [('feedback','feedback_count'),('repairs','repairs')]:
            if i[source] is not None:
                g[target]=(g[target] or 0)+i[source]
        for key2 in ('tokens','api_equivalent_cost_usd','unpriced_tokens','tool_calls','tool_failures','closed_waiting_seconds'):
            if i[key2] is not None:
                g[key2]=(g[key2] or 0)+i[key2]
        if i['human_interventions'] is not None:
            g['human_interventions']=(g['human_interventions'] or 0)+i['human_interventions']
            g['measurement_outcomes_observed']+=1
        g['usage_outcomes_observed']+=int(i['api_equivalent_cost_usd'] is not None)
        g['tool_outcomes_observed']+=int(i['tool_calls'] is not None)
    return {'status':'current' if items else 'not-observed','groups':list(grouped.values()),'outcomes':items[-100:],'outcomes_total':len(items),
        'basis':'Whole outcomes by last receipt; groups match work kind, workflow, actual model and policy revision. Cost includes only exclusive fully observed session joins; missing/shared attribution is unknown. Waiting counts closed recorded intervals only. Receipt span is elapsed time, not human attention.'}


# --------------------------------------------------------------------------- OA-USAGE-002
# One accounting path for workspace, project, repository, task and run usage. The
# first-party consumer owns the reporting map (which canonical Project and which
# declared repository belong to which workspace); every quantity is measured here.

REPORTING_INTERFACE = 'workspace-reporting-v1'
DETAIL_LIMIT = 100
BUCKET_BASIS = {'ad-hoc': 'unregistered-directories', 'remote': 'remote-sources', 'unattributed': 'imported-without-repository'}
SHARE_DENOMINATORS = {
    'token_share': 'measured tokens in the selected period, Shared/Unassigned included',
    'dollar_share': 'priced API-equivalent dollars in the selected period; unpriced tokens and dollars not split at this scope are excluded',
}
ACCOUNTING_METRICS = {'tokens': 'workspace_accounting_tokens', 'allocation': 'run_usage_allocation', 'token_share': 'contribution_share', 'dollar_share': 'contribution_share',
                      'acceptance_rate': 'review_acceptance_rate', 'tokens_per_delivered': 'cohort_tokens_per_delivered_outcome', 'human_accepted': 'successor_human_acceptance',
                      'repairs': 'successor_effort_and_repairs', 'run_wait_seconds': 'successor_effort_and_repairs'}
ACCOUNTING_BASIS = ('A workspace total is the union of its attributed measurements: exact run portions and whole exclusive sessions follow the run\'s '
    'recorded Project; other repository usage follows the repository\'s single workspace. Usage whose owners disagree or are unknown stays in '
    'Shared/Unassigned, so groups plus Shared/Unassigned equal the measured total. Repository and task rows overlap the workspace total and are '
    'never added to it. Unobserved activity is a coverage gap, not an amount. Dollars are never split by token share.')


def _reporting(value):
    if not isinstance(value, dict) or value.get('interface') != REPORTING_INTERFACE:
        return None
    text = lambda v: isinstance(v, str) and bool(v.strip())
    repositories = []
    for entry in value.get('repositories') or []:
        if isinstance(entry, dict) and text(entry.get('key')):
            repositories.append({'key': entry['key'], 'groups': sorted({g for g in entry.get('groups') or [] if text(g)}),
                                 'checkouts': [c for c in entry.get('checkouts') or [] if text(c)]})
    projects = {k: v for k, v in (value.get('projects') or {}).items() if text(k) and text(v)} if isinstance(value.get('projects'), dict) else {}
    return {'repositories': repositories, 'projects': projects, 'unclaimed': value.get('unclaimed') == 'own-group'}


def _receipt_tokens(vendor, value):
    if not isinstance(value, dict):
        return None
    number = lambda key: value.get(key) if isinstance(value.get(key), int) and not isinstance(value.get(key), bool) and value.get(key) >= 0 else None
    if number('input_tokens') is None or number('output_tokens') is None:
        return None
    keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens') if vendor == 'anthropic' else ('input_tokens', 'output_tokens')
    return sum(number(key) or 0 for key in keys)


def _portions(vendor, record):
    """Exact per-run usage inside one native session, from explicit receipt boundaries only."""
    rows = record['usage']
    if not rows:
        return None
    scopes = {r.get('usage_scope') for r in rows}
    if len(scopes) != 1 or not scopes <= {'turn', 'cumulative'}:
        return {'status': 'unknown', 'by_outcome': {}, 'dated': [], 'basis': None}
    scope = scopes.pop()
    status, by_outcome, dated, previous = 'exact', defaultdict(int), [], None
    first_bound = record['bound'][0] if record['bound'] else None
    for r in rows:
        total = _receipt_tokens(vendor, r.get('usage'))
        if total is None:
            status = 'partial'
            continue
        if scope == 'turn':
            portion = total
        elif previous is None:
            previous = total
            # Only a session this runtime started has a known zero baseline.
            if not (first_bound and first_bound[1] is False and first_bound[0] <= r['at']):
                status = 'partial'
                continue
            portion = total
        elif total >= previous:
            portion, previous = total - previous, total
        else:
            status, previous = 'partial', total  # counter reset: the interval is unknown, never zero
            continue
        stamp = usage.parse_timestamp(r['at'])
        by_outcome[r['outcome_id']] += portion
        dated.append((stamp.date().isoformat() if stamp else None, r['outcome_id'], portion))
    return {'status': status, 'by_outcome': dict(by_outcome), 'dated': dated, 'basis': 'exact-turn' if scope == 'turn' else 'exact-interval'}


def _managed_sessions(connection):
    sessions = {}
    for row in connection.execute('SELECT record_json FROM outcome_events WHERE vendor IS NOT NULL AND native_session_id IS NOT NULL AND outcome_id IS NOT NULL ORDER BY at,ledger_seq,event_id'):
        r = json.loads(row['record_json'])
        s = sessions.setdefault((r['vendor'], r['native_session_id']), {'outcomes': {}, 'usage': [], 'bound': []})
        if r.get('project_id') or r['outcome_id'] not in s['outcomes']:
            s['outcomes'][r['outcome_id']] = r.get('project_id') or s['outcomes'].get(r['outcome_id'])
        if r['kind'] == 'usage.observed':
            s['usage'].append(r)
        elif r['kind'] == 'session.bound':
            s['bound'].append((r['at'], r.get('adopted')))
    out = {}
    for (vendor, sid), s in sessions.items():
        stored = connection.execute('SELECT * FROM sessions WHERE vendor=? AND session_id=?', (vendor, sid)).fetchone()
        item = {'vendor': vendor, 'session_id': sid, 'outcomes': s['outcomes'], 'status': 'observed' if stored else 'not-observed', 'repository': None, 'registry_id': None,
                'tokens': None, 'api_equivalent_cost_usd': None, 'unpriced_tokens': None, 'allocation': 'unknown', 'portions': [], 'shared_tokens': None, 'unallocated_tokens': None, 'rows': []}
        if stored:
            seen = set()
            for obs in connection.execute('SELECT * FROM usage_observations WHERE vendor=? AND session_id=? ORDER BY event_id, file_id', (vendor, sid)):
                if obs['event_id'] not in seen:
                    seen.add(obs['event_id'])
                    item['rows'].append(obs)
            item.update(repository=stored['public_label'] or stored['project_code'], registry_id=stored['project_id'], api_equivalent_cost_usd=float(stored['cost_usd']), unpriced_tokens=int(stored['unpriced_tokens']),
                        tokens=usage.token_total(vendor, observatory.vendor_classes(vendor, json.loads(stored['tokens_json']))))
        split = _portions(vendor, s)
        item['_split'] = split
        if split is None:
            item['allocation'] = 'exclusive-session' if len(s['outcomes']) == 1 else 'unknown'
        elif split['status'] != 'unknown' and stored and sum(split['by_outcome'].values()) <= item['tokens']:
            item['allocation'] = 'partitioned' if split['status'] == 'exact' else 'partial'
            item['portions'] = [{'outcome_id': oid, 'project_id': s['outcomes'].get(oid), 'tokens': tokens, 'basis': split['basis']} for oid, tokens in split['by_outcome'].items()]
            remainder = item['tokens'] - sum(split['by_outcome'].values())
            item['shared_tokens' if split['status'] == 'exact' else 'unallocated_tokens'] = remainder
        if not stored:
            item['allocation'] = 'unknown'
            item['portions'] = []
        out[(vendor, sid)] = item
    return out


def _in_range(session, from_day, to_day):
    """Tokens (and the producer-priced dollars when the whole session lies inside) for one date range."""
    classes = {key: 0 for key in observatory.TOKEN_COLUMNS}
    inside = 0
    for row in session['rows']:
        day = row['day_utc']
        if day and (from_day is None or day >= from_day) and day <= to_day:
            inside += 1
            observatory.add_classes(classes, row)
    tokens = usage.token_total(session['vendor'], observatory.vendor_classes(session['vendor'], classes))
    whole = inside == len(session['rows'])
    return tokens, (session['api_equivalent_cost_usd'] if whole else None), (session['unpriced_tokens'] if whole else None)


def _owner(candidates, shared_basis):
    groups = {c[1] for c in candidates if c[0] == 'group'}
    specials = sorted({c for c in candidates if c[0] != 'group'})
    if len(groups) == 1 and not specials:
        return ('group', groups.pop())
    if not groups and len(specials) == 1:
        return specials[0]
    return ('shared', shared_basis)


def _cells(connection, days, now, sessions, repo_candidates, project_candidate):
    import portfolio
    bounds = portfolio.period_bounds(days, now)
    _, from_day, to_day = bounds
    periods = portfolio.project_periods(connection, days, now)
    by_repo = defaultdict(list)
    for s in sessions.values():
        if s['status'] == 'observed':
            by_repo[s['registry_id']].append(s)
    cells = []

    def cell(tokens, cost, unpriced, candidates, basis):
        if tokens or (cost is not None and abs(cost) > 1e-9):
            owner = _owner(candidates, basis)
            cells.append({'owner': owner, 'tokens': tokens, 'cost': cost, 'unpriced': unpriced, 'basis': basis})

    for registry_id in sorted(set(periods) | set(by_repo)):
        metrics = periods.get(registry_id) or {}
        repo_tokens, repo_cost, repo_unpriced = metrics.get('tokens') or 0, metrics.get('api_equivalent_cost_usd') or 0.0, metrics.get('unpriced_tokens') or 0
        repository = repo_candidates(registry_id)
        managed_tokens, managed_cost, managed_unpriced, unsplit_owners = 0, 0.0, 0, []
        for s in by_repo.get(registry_id, []):
            tokens, cost, unpriced = _in_range(s, from_day, to_day)
            if not tokens and not cost:
                continue
            owners = [project_candidate(pid) for pid in s['outcomes'].values()]
            managed_tokens += tokens
            if cost is None:
                unsplit_owners += repository + owners
            else:
                managed_cost += cost
                managed_unpriced += unpriced
            if _owner(repository + owners, 'x')[0] == 'group' or s['allocation'] == 'exclusive-session':
                whole = repository + owners if s['allocation'] != 'exclusive-session' else owners
                cell(tokens, cost, unpriced, whole, 'managed-session')
                continue
            split = s['_split']
            dated = defaultdict(int)
            for day, oid, portion in (split or {}).get('dated', []):
                if day and (from_day is None or day >= from_day) and day <= to_day:
                    dated[oid] += portion
            if s['allocation'] not in ('partitioned', 'partial') or sum(dated.values()) > tokens:
                cell(tokens, cost, unpriced, owners, 'shared-session-unknown-allocation')
                continue
            for oid, portion in dated.items():
                cell(portion, None, None, [project_candidate(s['outcomes'].get(oid))], 'run-portion')
            cell(tokens - sum(dated.values()), None, None, repository + owners, 'session-remainder' if s['allocation'] == 'partitioned' else 'session-unallocated')
            if cost is not None:
                cell(0, cost, unpriced, repository + owners, 'session-dollars-not-split')
        base_tokens = repo_tokens - managed_tokens
        if not unsplit_owners:
            cell(base_tokens, round(repo_cost - managed_cost, 6), repo_unpriced - managed_unpriced, repository, 'repository-usage')
        else:
            cell(base_tokens, None, None, repository, 'repository-usage')
            cell(0, round(repo_cost - managed_cost, 6), repo_unpriced - managed_unpriced, repository + unsplit_owners, 'repository-dollars-with-unsplit-sessions')
    return cells, {'days': bounds[0], 'from_day': from_day, 'to_day': to_day}


def _metrics(cells, totals=None):
    priced = [c for c in cells if c['cost'] is not None]
    tokens = sum(c['tokens'] for c in cells)
    without = sum(c['tokens'] for c in cells if c['cost'] is None)
    cost = round(sum(c['cost'] for c in priced), 6) if priced else (None if without else 0.0)
    value = {'tokens': tokens, 'api_equivalent_cost_usd': cost, 'unpriced_tokens': sum(c['unpriced'] or 0 for c in priced), 'tokens_without_dollars': without,
             # Observed usage outside managed runs stays distinguishable from managed work.
             'outside_managed_run_tokens': sum(c['tokens'] for c in cells if c['basis'] == 'repository-usage'),
             'managed_run_tokens': sum(c['tokens'] for c in cells if c['basis'] != 'repository-usage')}
    if totals is not None:
        value['token_share'] = round(tokens / totals['tokens'], 4) if totals['tokens'] else None
        value['dollar_share'] = round(cost / totals['api_equivalent_cost_usd'], 4) if cost is not None and totals['api_equivalent_cost_usd'] else None
    return value


def _measures(items):
    known = lambda key: [i[key] for i in items if i.get(key) is not None]
    total = lambda key: sum(known(key)) if known(key) else None
    delivered = sum(bool(i['delivered']) for i in items)
    reviewed = sum(i['verdict'] in ('accepted', 'needs-changes') for i in items)
    accepted = sum(i['verdict'] == 'accepted' for i in items)
    attributed = [i for i in items if i['allocation'] == 'exact']
    complete = bool(items) and len(attributed) == len(items) and delivered > 0
    contexts = {(i['outcome_kind'], i['workflow_identity'], i['model'], i['policy_revision']) for i in items}
    tokens = sum(i['attributed_tokens'] for i in attributed)
    return {
        'outcomes': len(items), 'delivered': delivered,
        'human_accepted': sum(bool(i['delivered']) and i['verdict'] == 'accepted' and i['acceptance_basis'] == 'human' for i in items),
        'needs_changes': sum(i['verdict'] == 'needs-changes' for i in items), 'reviewed': reviewed,
        'delivered_unreviewed': sum(bool(i['delivered']) and i['verdict'] is None for i in items),
        'acceptance_rate': round(accepted / reviewed, 4) if reviewed else None,
        'acceptance_denominator': 'outcomes with an explicit review verdict',
        'attempts': total('attempts'), 'repairs': total('repairs'), 'human_interventions': total('human_interventions'),
        'measurement_outcomes_observed': sum(i['human_interventions'] is not None for i in items),
        'run_wait_seconds': total('closed_waiting_seconds'), 'open_waits': sum(i['open_waits'] or 0 for i in items),
        'cost_cohort': {'outcomes': len(items), 'attributed': len(attributed), 'tokens': tokens if attributed else None,
                        'tokens_per_delivered': round(tokens / delivered, 1) if complete else None,
                        'status': 'complete' if complete else 'insufficient-evidence', 'comparable': len(contexts) <= 1,
                        'reason': f'Usage is exactly attributed for {len(attributed)} of {len(items)} outcomes' + ('' if delivered else '; none delivered')
                                  + ('' if len(contexts) <= 1 else '; mixed work kinds, workflows, models or policies, so this is descriptive only and not an efficiency comparison')},
    }


MAX_RUN_USAGE = 200


def _run_usage(items, sessions, requested):
    """Run-scoped figures for the requested outcomes: a whole-session total never reads as a run's."""
    wanted = {str(x) for x in list(requested or [])[:MAX_RUN_USAGE] if isinstance(x, str)}
    out = {}
    for item in items:
        if item['outcome_id'] not in wanted or not item['sessions']:
            continue
        bound = [sessions.get((s['vendor'], s['session_id'])) or {} for s in item['sessions']]
        observed = all(s.get('status') == 'observed' for s in bound)
        entry = {k: item.get(k) for k in ('outcome_id', 'project_id', 'group', 'allocation', 'attempts', 'repair_of', 'delivered', 'verdict', 'acceptance_basis', 'feedback_count', 'repairs', 'human_interventions', 'last_at')}
        entry.update(tokens=item['attributed_tokens'] if item['allocation'] in ('exact', 'partial') else None,
                     session_tokens=sum(s['tokens'] for s in bound) if observed else None, sessions=item['sessions'],
                     api_equivalent_cost_usd=None, unpriced_tokens=None, token_classes=None, pricing='unknown')
        if item['allocation'] == 'exact' and observed and all(s.get('allocation') == 'exclusive-session' for s in bound):
            classes = defaultdict(int)
            for s in bound:
                for rows in (s['rows'],):
                    total = {key: 0 for key in observatory.TOKEN_COLUMNS}
                    for row in rows:
                        observatory.add_classes(total, row)
                    for key, value in observatory.vendor_classes(s['vendor'], total).items():
                        classes[key] += value
            unpriced = sum(s['unpriced_tokens'] for s in bound)
            entry.update(api_equivalent_cost_usd=round(sum(s['api_equivalent_cost_usd'] for s in bound), 6), unpriced_tokens=unpriced, token_classes=dict(classes),
                         pricing='unpriced' if entry['tokens'] and unpriced >= entry['tokens'] else 'partly-priced' if unpriced else 'priced')
        elif item['allocation'] in ('exact', 'partial'):
            entry['pricing'] = 'not-split'
        out[item['outcome_id']] = entry
    return out


def accounting_view(connection, *, reporting=None, days=None, now=None, registry=None, attention=(), coverage=None, detail=None, run_outcomes=None):
    import datetime as dt
    import portfolio
    now = now or dt.datetime.now(dt.timezone.utc)
    base = {'status': 'unavailable', 'run_usage': {}, 'previous_period': None, 'metrics': dict(ACCOUNTING_METRICS), 'groups': [], 'shared': None, 'totals': None, 'repositories': [], 'sessions': [], 'sessions_total': 0, 'outcomes': [], 'outcomes_total': 0,
            'share_denominators': dict(SHARE_DENOMINATORS), 'basis': ACCOUNTING_BASIS, 'coverage_gaps': []}
    if not portfolio._table_exists(connection, 'portfolio_daily') or portfolio.period_bounds(days, now) is None:
        return {**base, 'detail_code': 'store_schema_before_portfolio' if portfolio.period_bounds(days, now) else 'unsupported_window'}
    reporting = _reporting(reporting)
    projects_rows = {row['project_id']: row for row in connection.execute('SELECT project_id, project_code, public_label, category FROM projects')}
    public = lambda registry_id: (projects_rows[registry_id]['public_label'] or projects_rows[registry_id]['project_code']) if registry_id in projects_rows else registry_id
    keys_by_measured, resolutions = defaultdict(set), defaultdict(list)
    groups_by_key = {entry['key']: entry['groups'] for entry in (reporting or {}).get('repositories', [])}
    for entry in (reporting or {}).get('repositories', []):
        for path in entry['checkouts']:
            resolved = observatory.resolve_project(path, '', registry or {'public': {}, 'mappings': [], 'tail_rules': []}, '')
            if resolved['registered'] and resolved['project_id'] not in observatory.BUCKET_IDS:
                keys_by_measured[resolved['project_id']].add(entry['key'])
                resolutions[resolved['project_id']].append({'checkout': path, 'resolution': resolved['resolution']})

    def repo_candidates(registry_id):
        if registry_id in BUCKET_BASIS:
            return [('unassigned', BUCKET_BASIS[registry_id])]
        keys = sorted(keys_by_measured.get(registry_id, ()))
        if len(keys) > 1:
            return [('shared', 'repository-identity-conflict')]
        if keys:
            groups = groups_by_key.get(keys[0]) or []
            return [('group', groups[0])] if len(groups) == 1 else [('shared', 'repository-in-several-workspaces')] if groups else [('unassigned', 'repository-without-workspace')]
        return [('group', 'repository:' + public(registry_id))] if reporting and reporting['unclaimed'] else [('unassigned', 'repository-without-workspace')]

    def project_candidate(project_id):
        group = (reporting or {}).get('projects', {}).get(project_id)
        return ('group', group) if group else ('unassigned', 'run-without-workspace')

    sessions = _managed_sessions(connection)
    period_cells, bounds = _cells(connection, days, now, sessions, repo_candidates, project_candidate)
    lifetime_cells = period_cells if bounds['from_day'] is None else _cells(connection, 'all', now, sessions, repo_candidates, project_candidate)[0]
    # The preceding equal-length window, for change statements over the same definitions.
    previous_cells, previous_bounds = (None, None) if bounds['from_day'] is None else _cells(connection, days, now - dt.timedelta(days=bounds['days']), sessions, repo_candidates, project_candidate)
    by_outcome, native_owners = _receipts(connection)
    items = _outcome_items(connection, by_outcome, native_owners)
    for item in items:
        rows = by_outcome[item['outcome_id']]
        attempt_ids = {r['attempt_id'] for r in rows if r.get('attempt_id')}
        bindings = {r['event_id'] for r in rows if r['kind'] == 'session.bound'}
        item['attempts'] = len(attempt_ids) if attempt_ids else (len(bindings) or None)
        item['repair_of'] = next((r['repair_of'] for r in rows if r.get('repair_of')), None)
        pairs = sorted({(r['vendor'], r['native_session_id']) for r in rows if r.get('vendor') and r.get('native_session_id')})
        attributed, allocation = 0, 'exact' if pairs else 'not-bound'
        for pair in pairs:
            s = sessions.get(pair)
            if s is None or s['allocation'] == 'unknown':
                attributed, allocation = None, 'unknown'
                break
            if s['allocation'] == 'exclusive-session':
                attributed += s['tokens']
            else:
                attributed += next((p['tokens'] for p in s['portions'] if p['outcome_id'] == item['outcome_id']), 0)
                allocation = 'partial' if s['allocation'] == 'partial' else allocation
        item['attributed_tokens'] = attributed if pairs else None
        item['allocation'] = allocation
        def measured_share(pair):
            s = sessions.get(pair) or {}
            if s.get('allocation') == 'exclusive-session':
                return s.get('tokens')
            if s.get('allocation') in ('partitioned', 'partial'):
                return next((p['tokens'] for p in s['portions'] if p['outcome_id'] == item['outcome_id']), 0)
            return None
        item['sessions'] = [{'vendor': v, 'session_id': sid, 'allocation': (sessions.get((v, sid)) or {}).get('allocation', 'unknown'), 'tokens': measured_share((v, sid)), 'session_tokens': (sessions.get((v, sid)) or {}).get('tokens')} for v, sid in pairs]
        owner = project_candidate(item['project_id'])
        item['group'] = owner[1] if owner[0] == 'group' else None

    group_keys = set((reporting or {}).get('projects', {}).values()) | {g for entry in (reporting or {}).get('repositories', []) for g in entry['groups']}
    group_keys |= {c['owner'][1] for c in period_cells + lifetime_cells + (previous_cells or []) if c['owner'][0] == 'group'}
    measured_ids = sorted(set(projects_rows) | {s['registry_id'] for s in sessions.values() if s['registry_id']})
    if reporting and reporting['unclaimed']:
        group_keys |= {repo_candidates(r)[0][1] for r in measured_ids if repo_candidates(r)[0][0] == 'group'}
    totals = {'period': _metrics(period_cells), 'lifetime': _metrics(lifetime_cells), 'previous': _metrics(previous_cells) if previous_cells is not None else None}
    attention_by_group = defaultdict(lambda: defaultdict(float))
    for interval in attention:
        day = interval.get('day')
        if not day or (bounds['from_day'] is not None and day < bounds['from_day']) or day > bounds['to_day']:
            continue
        candidates = repo_candidates(interval['project_id']) if interval['project_id'] in keys_by_measured or interval['project_id'] in projects_rows else [project_candidate(interval['project_id'])]
        owner = _owner(candidates, 'attention')
        if owner[0] == 'group':
            attention_by_group[owner[1]][interval['mode']] += interval['attention_seconds']
    groups = []
    for key in sorted(group_keys) if reporting else []:
        mine = lambda cells: [c for c in cells if c['owner'] == ('group', key)]
        members = [i for i in items if i['group'] == key]
        recorded = attention_by_group.get(key)
        groups.append({'key': key, 'period': _metrics(mine(period_cells), totals['period']), 'lifetime': _metrics(mine(lifetime_cells), totals['lifetime']),
                       'previous': _metrics(mine(previous_cells), totals['previous']) if previous_cells is not None else None,
                       'outcomes': _measures(members),
                       'attention': {'recorded_seconds': round(sum(recorded.values()), 3), 'by_mode': dict(recorded), 'basis': 'explicitly recorded timer intervals joined at workspace level'} if recorded else None})

    def shared(cells, scope_totals):
        rest = [c for c in cells if c['owner'][0] != 'group']
        parts = defaultdict(list)
        for c in rest:
            parts[c['owner'][1]].append(c)
        return {**_metrics(rest, scope_totals), 'parts': [{'basis': basis, **_metrics(values)} for basis, values in sorted(parts.items())]}

    repositories = []
    for registry_id in measured_ids:
        keys = sorted(keys_by_measured.get(registry_id, ()))
        candidate = repo_candidates(registry_id)[0]
        association = 'bucket' if registry_id in BUCKET_BASIS or registry_id in observatory.BUCKET_IDS else 'conflict' if len(keys) > 1 else ('grouped' if candidate[0] == 'group' else 'shared' if candidate[1] == 'repository-in-several-workspaces' else 'without-workspace') if keys else ('unclaimed' if candidate[0] == 'group' else 'without-workspace')
        row = projects_rows.get(registry_id)
        repositories.append({'repository': public(registry_id), 'label': row['public_label'] if row else None, 'category': row['category'] if row else None, 'repository_keys': keys,
                             'groups': sorted({g for k in keys for g in groups_by_key.get(k, [])}) if keys else ([candidate[1]] if candidate[0] == 'group' else []),
                             'association': association, 'resolutions': resolutions.get(registry_id, [])})

    detail = detail if isinstance(detail, dict) else {}
    offset = detail.get('offset') if isinstance(detail.get('offset'), int) and not isinstance(detail.get('offset'), bool) and detail.get('offset') >= 0 else 0
    limit = detail.get('limit') if isinstance(detail.get('limit'), int) and not isinstance(detail.get('limit'), bool) and 0 <= detail.get('limit') <= DETAIL_LIMIT else DETAIL_LIMIT
    selected = detail.get('group') if isinstance(detail.get('group'), str) else None
    chosen = sorted((i for i in items if selected is None or i['group'] == selected), key=lambda i: (i['last_at'], i['outcome_id']), reverse=True)
    related = [s for s in sessions.values() if selected is None or any(project_candidate(pid) == ('group', selected) for pid in s['outcomes'].values()) or (s['registry_id'] and repo_candidates(s['registry_id']) == [('group', selected)])]
    related.sort(key=lambda s: (str(max((r['timestamp_utc'] or '' for r in s['rows']), default='')), s['session_id']), reverse=True)
    public_session = lambda s: {k: v for k, v in s.items() if k not in ('rows', '_split', 'registry_id')}
    coverage = coverage or {}
    gaps = [{'source': 'import', 'import_id': i['import_id'], 'status': i['status'], 'detail_code': i['detail_code']} for i in coverage.get('imports', []) if i.get('status') != 'current']
    gaps += [{'source': 'usage-source', 'source_key': s['source_key'], 'status': s['status'], 'detail_code': s['detail_code']} for s in coverage.get('sources', []) if s.get('status') in ('unavailable', 'stale', 'partial', 'never-observed')]
    unobserved = sum(s['status'] != 'observed' for s in sessions.values())
    if unobserved:
        gaps.append({'source': 'managed-sessions', 'status': 'not-observed', 'count': unobserved, 'detail_code': 'bound_native_session_without_measurement'})
    return {**base, 'status': 'current', 'period': bounds, 'groups': groups,
            'shared': {'period': shared(period_cells, totals['period']), 'lifetime': shared(lifetime_cells, totals['lifetime']),
                       'previous': shared(previous_cells, totals['previous']) if previous_cells is not None else None},
            'previous_period': previous_bounds, 'run_usage': _run_usage(items, sessions, run_outcomes),
            'totals': totals, 'repositories': repositories,
            'sessions': [public_session(s) for s in related[offset:offset + limit]], 'sessions_total': len(related),
            'outcomes': chosen[offset:offset + limit], 'outcomes_total': len(chosen),
            'detail': {'group': selected, 'offset': offset, 'limit': limit}, 'coverage_gaps': gaps}
