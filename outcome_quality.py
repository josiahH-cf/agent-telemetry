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


def quality_view(connection, *, project_id=None, days=None):
    """One receipt-backed row per outcome, plus compact comparable-class totals.

No scan, provider call, prompt body or source path is returned. The receipt
range bounds elapsed/wait evidence; it is not a human-attention measurement.
"""
    by_outcome=defaultdict(list)
    native_owners=defaultdict(set)
    for row in connection.execute('SELECT record_json FROM outcome_events WHERE outcome_id IS NOT NULL ORDER BY at,ledger_seq,event_id'):
        r=json.loads(row['record_json'])
        oid=r['outcome_id']
        by_outcome[oid].append(r)
        if r.get('native_session_id') and r.get('vendor'):
            native_owners[(r['vendor'],r['native_session_id'])].add(oid)
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
