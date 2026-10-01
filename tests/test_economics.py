"""AE-01: attribution unions, privacy, dated interpretation and unknown states."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import code_evidence
import consumer
import economics
import metric_catalog
import observatory
import outcomes
from tools import attention
from tests.test_portfolio import NOW, PROJECT_ROOT, config_for, root_path, state_of, write_lines
from tests.test_workspace_accounting import S1, S2, claude_session, receipt

UTC = dt.timezone.utc


def snapshot(projects=20):
    codes = [f'proj-{i:08d}' for i in range(projects)]
    daily = [{'date': f'2026-09-{day:02d}', 'project_id': pid, 'cost_usd': 1.0, 'unpriced_tokens': 0,
              'tokens': 10, 'sessions': 1, 'vendor': 'openai', 'host_os': 'wsl'}
             for day in range(17, 31) for pid in codes]
    daily += [{'date': '2026-09-30', 'project_id': 'ad-hoc', 'cost_usd': 5, 'unpriced_tokens': 9,
               'tokens': 9, 'sessions': 1, 'vendor': 'openai', 'host_os': 'wsl'}]
    timed = [{'date': day, 'project_id': codes[-1], 'attention_seconds': seconds,
              'mode_seconds': {m: seconds if m == mode else 0 for m in attention.MODES},
              'interval_segments': 1, 'transitions_in': 0, 'source': 'operator_timer'}
             for day, seconds, mode in [('2026-09-17', 3600, 'guide'), ('2026-09-30', 7200, 'plan')]]
    facts = [{'outcome_id': f'outc-{i:016x}', 'project_id': pid, 'project_attribution': 'correlated',
              'first_at': '2026-09-17T00:00:00Z', 'last_at': '2026-09-30T10:00:00Z',
              'delivered': True, 'review_verdict': 'accepted' if i % 2 else None,
              'acceptance_basis': 'human' if i % 2 else None,
              'checks_passed': 1, 'checks_failed': 0, 'verification_observed': True,
              'attempts': 2, 'repairs': 1, 'human_interventions': 0,
              'measurement_since': '2026-09-17T00:00:00Z', 'measurement_complete': True,
              'api_equivalent_cost_usd': 99999} for i, pid in enumerate(codes)]
    facts += [{**facts[0], 'outcome_id': 'outc-ffffffffffffffff', 'project_id': None, 'project_attribution': 'shared'}]
    return {'generated_at': '2026-10-01T12:00:00Z', 'collection': {'date': '2026-10-01'},
            'metrics': {'observatory': {'projects': [{'project_id': c, 'project_code': c} for c in codes], 'daily': daily},
                        'attention': {'days': timed, 'status': 'available', 'publication_enabled': True,
                                      'coverage': {'from': '2026-09-17', 'to': '2026-09-30'}},
                        'economics': {'outcomes': facts, 'outcome_status': 'observed', 'code_changes': [],
                                      'code_coverage': {'status': 'not-configured'}}}}


class ReportTests(unittest.TestCase):
    def test_union_tail_shared_and_trend_reconcile_without_adding_outcome_cost(self):
        report = economics.report(snapshot(), '7')
        self.assertEqual(report['period'], {'from': '2026-09-24', 'to': '2026-09-30', 'closed_utc': True})
        self.assertEqual(report['previous_period']['to'], '2026-09-23')
        rows = report['projects'] + [report['shared']]
        self.assertEqual(len(report['projects']), 7)
        self.assertEqual(report['projects'][-1]['other_count'], 14)
        self.assertEqual(sum(r['api_equivalent_cost_usd'] or 0 for r in rows), 145)
        self.assertEqual(report['totals']['api_equivalent_cost_usd'], 145)
        self.assertEqual(sum(r['recorded_attention_seconds'] or 0 for r in rows), 7200)
        self.assertEqual(sum(r['results']['delivered'] for r in rows), 21)
        self.assertEqual(report['shared']['attribution'], {'shared': 1})
        self.assertEqual(sum(b['api_equivalent_cost_usd'] or 0 for b in report['trend']), 145)
        self.assertEqual(sum(b['delivered'] or 0 for b in report['trend']), 21)
        self.assertEqual(report['change']['values']['api_equivalent_cost_usd'], 5)
        self.assertEqual(report['change']['values']['recorded_attention_hours'], 1)
        self.assertIsNone(report['worth']['assessment'])

    def test_missing_and_observed_zero_are_distinct(self):
        value = snapshot()
        report = economics.report(value, '7')
        self.assertEqual(report['totals']['mode_seconds']['rework'], 0)
        self.assertIsNone(report['totals']['code']['revisions'])
        value['metrics']['attention']['days'] = []
        value['metrics']['attention']['coverage'] = {'from': None, 'to': None}
        report = economics.report(value, '7')
        self.assertIsNone(report['totals']['recorded_attention_seconds'])
        self.assertIsNone(report['totals']['mode_seconds']['rework'])
        self.assertIsNone(report['change']['values']['recorded_attention_hours'])
        self.assertIsNone(report['actual_cash']['amount_usd'])
        self.assertEqual(report['totals']['results']['human_interventions'], 0)

    def test_before_outcome_coverage_and_partial_code_cannot_compare(self):
        report = economics.report(snapshot(), '30')
        self.assertIsNone(report['previous']['results']['delivered'])
        self.assertIsNone(report['change']['values']['delivered'])
        self.assertEqual(report['change']['reasons']['delivered'], 'incomplete_or_missing_coverage')
        self.assertIsNone(economics.report(snapshot(), 'all')['previous'])

    def test_project_without_outcome_or_code_capture_stays_unknown(self):
        value = snapshot(2)
        facts = value['metrics']['economics']
        facts['outcomes'] = [r for r in facts['outcomes'] if r.get('project_id') != 'proj-00000001']
        facts['code_coverage'] = {'status': 'current', 'from': '2026-01-01', 'to': '2026-10-01',
                                 'by_project': {'proj-00000000': {'status': 'current', 'from': '2026-01-01'}}}
        report = economics.report(value, '7')
        observed, unknown = sorted(report['projects'], key=lambda r: r['project_code'])
        self.assertEqual(observed['code']['revisions'], 0)
        self.assertIsNone(unknown['code']['revisions'])
        self.assertIsNone(unknown['results']['delivered'])
        self.assertIsNone(unknown['change']['values']['delivered'])

    def test_partial_capture_has_separate_qualified_observed_difference(self):
        value = snapshot()
        value['metrics']['observatory']['source_roots'] = [{'status': 'partial'}]
        change = economics.report(value, '7')['change']
        self.assertIsNone(change['values']['api_equivalent_cost_usd'])
        self.assertEqual(change['observed_values']['api_equivalent_cost_usd'], 5)
        self.assertEqual(change['reasons']['api_equivalent_cost_usd'], 'incomplete_or_missing_coverage')

    def test_high_cardinality_payload_stays_bounded_and_contains_no_operator_prose(self):
        page = metric_catalog.build_page_envelope(snapshot(1000))
        for window in page['windows'].values():
            report = window['investment_results']
            self.assertLessEqual(len(report['projects']), 7)
            self.assertLessEqual(len(report['trend']), 48)
        self.assertLess(len(metric_catalog.page_payload_text(page).encode()), metric_catalog.PAGE_TARGET_BYTES)
        self.assertNotIn('native_session_id', json.dumps(page))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'projects.json').write_text(json.dumps({'projects': [{'project_id': 'proj-00000019', 'public_label': None}]}))
        self.state = self.root / 'state'
        self.now = dt.datetime(2026, 10, 1, 12, tzinfo=UTC)

    def tearDown(self):
        self.temp.cleanup()

    def context(self, revision=1, **extra):
        return {'kind': 'context', 'entry_id': 'goal-one', 'revision': revision, 'project_id': 'proj-00000019',
                'effective_at': '2026-09-30T01:00:00Z', 'provenance': 'operator supplied', 'status': 'active',
                'goal': 'PRIVATE_GOAL_SENTINEL', 'context': 'PRIVATE_CONTEXT_SENTINEL',
                'goal_metric': 'delivered', 'goal_target': 1, **extra}

    def test_context_revisions_as_of_effective_dates_and_explicit_goal(self):
        record = self.context(effective_at='2026-09-29T23:00:00-02:00')
        economics.append_record(self.project, self.state, record, self.now)
        economics.append_record(self.project, self.state, self.context(2, status='completed'), self.now+dt.timedelta(hours=1))
        report = economics.report(snapshot(), '7')
        rows, _ = economics._aggregate(snapshot(), report['period'], report['coverage'])
        first = economics.local_context(self.state, report['period'], '2026-10-01T12:30:00Z', rows)
        context = first['contexts']['proj-00000019']
        self.assertEqual(context['revision'], 1)
        self.assertEqual(context['effective_at'], '2026-09-30T01:00:00Z')
        self.assertTrue(context['goal_assessment']['met'])
        latest = economics.local_context(self.state, report['period'], '2026-10-01T14:00:00Z', rows)
        self.assertEqual(latest['contexts']['proj-00000019']['revision'], 2)
        past = economics.local_context(self.state, {'from': '2026-09-01', 'to': '2026-09-29'}, '2026-10-01T14:00:00Z', rows)
        self.assertEqual(past['contexts'], {})
        self.assertNotIn('PRIVATE_GOAL_SENTINEL', json.dumps(report))

    def test_cash_zero_dedup_revisions_and_api_separation(self):
        cash = {'kind': 'cash', 'entry_id': 'payment-one', 'revision': 1, 'project_id': None,
                'effective_at': '2026-09-30T00:00:00Z', 'date': '2026-09-30', 'provenance': 'operator invoice entry',
                'amount_cents': 0, 'category': 'subscription'}
        self.assertEqual(economics.append_record(self.project, self.state, cash, self.now), 'recorded')
        self.assertEqual(economics.append_record(self.project, self.state, cash, self.now), 'already-recorded')
        report = economics.report(snapshot(), '7')
        local = economics.local_context(self.state, report['period'], '2026-10-01T13:00:00Z', [])
        self.assertEqual(local['actual_cash']['amount_usd'], 0)
        economics.append_record(self.project, self.state, {**cash, 'revision': 2, 'amount_cents': 1234}, self.now)
        local = economics.local_context(self.state, report['period'], '2026-10-01T13:00:00Z', [])
        self.assertEqual(local['actual_cash']['amount_usd'], 12.34)
        self.assertEqual(local['actual_cash']['entries'], 1)
        self.assertEqual(report['totals']['api_equivalent_cost_usd'], 145)
        with self.assertRaises(ValueError):
            economics.append_record(self.project, self.state, {**cash, 'amount_cents': 10}, self.now)
        self.assertEqual((self.state/economics.LEDGER).stat().st_mode & 0o777, 0o600)

    def test_report_archive_is_immutable_after_context_change(self):
        value = snapshot()
        economics.append_record(self.project, self.state, self.context(), self.now)
        economics.archive_reports(self.project, self.state, value)
        path = self.state/'economics-reports/2026-09-30.json'
        before = path.read_bytes()
        economics.append_record(self.project, self.state, self.context(2, status='completed'), self.now)
        economics.archive_reports(self.project, self.state, {**value, 'generated_at': '2026-10-01T14:00:00Z'})
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual((path.parent/'latest.json').read_bytes(), before)
        private_html = path.parent/'latest.html'
        self.assertEqual(private_html.stat().st_mode & 0o777, 0o600)
        self.assertIn('PRIVATE_GOAL_SENTINEL', private_html.read_text())
        self.assertNotIn('PRIVATE_GOAL_SENTINEL', json.dumps(economics.report(value, '7')))

    def test_context_html_is_escaped_and_has_no_external_requests(self):
        economics.append_record(self.project, self.state, self.context(context='<script>PRIVATE_CONTEXT_SENTINEL</script>'), self.now)
        economics.archive_reports(self.project, self.state, snapshot())
        private_html = (self.state/'economics-reports/latest.html').read_text()
        self.assertIn('&lt;script&gt;PRIVATE_CONTEXT_SENTINEL&lt;/script&gt;', private_html)
        self.assertNotIn('<script', private_html)
        self.assertNotIn('src=', private_html)

    def test_undated_subscription_is_not_historical_cash_and_dates_prorate(self):
        path = self.project/'subscriptions.local.json'
        path.write_text(json.dumps({'monthly_usd': {'openai': 300}}))
        bounds = {'from': '2026-09-24', 'to': '2026-09-30'}
        undated = economics.subscription_view(self.project, bounds)
        self.assertEqual(undated['current_monthly_usd'], 300)
        self.assertIsNone(undated['estimate_usd'])
        path.write_text(json.dumps({'monthly_usd': {'openai': 300}, 'periods': [
            {'from': '2026-09-01', 'to': None, 'monthly_usd': {'openai': 300}}]}))
        self.assertEqual(economics.subscription_view(self.project, bounds)['estimate_usd'], 70)
        path.write_text(json.dumps({'periods': [{'from': '2026-09-25', 'monthly_usd': {'openai': 300}}]}))
        partial = economics.subscription_view(self.project, bounds)
        self.assertIsNone(partial['estimate_usd'])
        self.assertEqual(partial['covered_estimate_usd'], 60)
        self.assertEqual(partial['missing_vendor_days'], 1)

    def test_unregistered_context_and_extra_fields_are_rejected(self):
        for record in (self.context(project_id='private-name'), self.context(prompt='CONTENT_SENTINEL'), self.context(goal_metric='tokens')):
            with self.assertRaises(ValueError):
                economics.append_record(self.project, self.state, record, self.now)
        self.assertFalse((self.state/economics.LEDGER).exists())


class GitEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', '@'.join(('fixture', 'users.noreply.github.com')))
        self.git('config', 'user.name', 'fixture')
        for index in range(5):
            (self.repo/'PRIVATE_FILENAME_SENTINEL').write_text('PRIVATE_CODE_SENTINEL\n'*(index+1))
            self.git('add', '.')
            self.git('commit', '-qm', 'PRIVATE_MESSAGE_SENTINEL')
        self.con = observatory.connect_store(self.root/'store.sqlite3')
        self.registry = {'public': {'fixture-project': {'public_label': 'fixture-project'}}, 'tail_rules': [],
                         'mappings': [{'canonical_path': str(self.repo), 'project_id': 'fixture-project', 'public_label': 'fixture-project', 'category': 'project', 'match': 'exact'}]}
        self.config = {'observatory': {'code_roots': [{'root_id': 'fixture_code', 'project_id': 'fixture-project', 'path': str(self.repo), 'max_commits': 2}]}}

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_bounded_backfill_idempotent_private_metadata_and_missing_source(self):
        before = self.git('status', '--porcelain')
        self.assertEqual(code_evidence.collect(self.con, self.config, self.registry, '', NOW)[0]['status'], 'partial')
        self.assertEqual(len(code_evidence.public_rows(self.con)), 2)
        code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        self.assertEqual(len(code_evidence.public_rows(self.con)), 4)
        self.assertEqual(code_evidence.collect(self.con, self.config, self.registry, '', NOW)[0]['status'], 'current')
        code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        rows = code_evidence.public_rows(self.con)
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(r['insertions'] for r in rows), 5)
        payload = json.dumps(rows)
        for sentinel in ('PRIVATE_FILENAME_SENTINEL', 'PRIVATE_CODE_SENTINEL', 'PRIVATE_MESSAGE_SENTINEL', str(self.repo), 'native_revision'):
            self.assertNotIn(sentinel, payload)
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.repo.rename(self.root/'offline')
        result = code_evidence.collect(self.con, self.config, self.registry, '', NOW)[0]
        self.assertEqual(result['status'], 'retained-last-good')
        self.assertEqual(code_evidence.public_rows(self.con), rows)

    def test_new_commit_once_and_identity_conflict_retains_evidence(self):
        for _ in range(3):
            code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        (self.repo/'PRIVATE_FILENAME_SENTINEL').write_text('PRIVATE_CODE_SENTINEL\n'*6)
        self.git('commit', '-qam', 'PRIVATE_MESSAGE_SENTINEL')
        code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        self.assertEqual(len(code_evidence.public_rows(self.con)), 6)
        self.config['observatory']['code_roots'][0]['project_id'] = 'unknown'
        self.assertEqual(code_evidence.collect(self.con, self.config, self.registry, '', NOW)[0]['detail_code'], 'code_project_unregistered')
        self.assertEqual(len(code_evidence.public_rows(self.con)), 6)

    def test_rebuild_retains_metadata_when_source_is_offline(self):
        for _ in range(3):
            code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        rebuilt = observatory.connect_store(self.root/'rebuilt.sqlite3')
        try:
            code_evidence.preserve_rebuild_history(self.root/'store.sqlite3', rebuilt)
            self.repo.rename(self.root/'offline')
            self.assertEqual(code_evidence.collect(rebuilt, self.config, self.registry, '', NOW)[0]['status'], 'retained-last-good')
            self.assertEqual(code_evidence.public_rows(rebuilt), code_evidence.public_rows(self.con))
        finally:
            rebuilt.close()

    def test_code_only_project_preserves_join_without_inventing_provider_history(self):
        code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        economics.ensure_evidence_projects(self.con, self.registry, {})
        public, _ = observatory.machine_datasets(self.con)
        project = public['projects'][0]
        self.assertEqual(project['sessions'], 0)
        self.assertIsNone(project['first_seen_at'])
        self.assertTrue(all(r['project_id'] == project['project_code'] for r in public['code_changes']))

    def test_duplicate_checkouts_and_rewritten_history_do_not_erase_evidence(self):
        clone = self.root/'copy'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(clone)], check=True, capture_output=True)
        self.registry['mappings'].append({**self.registry['mappings'][0], 'canonical_path': str(clone)})
        self.config['observatory']['code_roots'].append({'root_id': 'fixture_copy', 'project_id': 'fixture-project', 'path': str(clone), 'max_commits': 2})
        for _ in range(3):
            code_evidence.collect(self.con, self.config, self.registry, '', NOW)
        self.assertEqual(len(code_evidence.public_rows(self.con)), 5)
        self.git('checkout', '--orphan', 'replacement')
        self.git('commit', '-qm', 'PRIVATE_MESSAGE_SENTINEL')
        self.assertEqual(code_evidence.collect(self.con, self.config, self.registry, '', NOW)[0]['detail_code'], 'code_history_rewritten')
        self.assertEqual(len(code_evidence.public_rows(self.con)), 5)

    def test_old_store_and_unavailable_root_do_not_claim_observed_zero(self):
        self.con.execute('DROP TABLE code_observations')
        self.con.execute('DROP TABLE code_roots')
        self.assertEqual(code_evidence.public_rows(self.con), [])
        self.assertEqual(code_evidence.coverage(self.con)['status'], 'not-configured')


class NativeAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = config_for(self.base, 'personal', producer='fixture-personal', environment='personal', account=None, host=None)
        self.config['observatory']['registry_paths'] = [
            {'path': '/fixture/repo-a', 'project_id': 'obsidian-agent'}, {'path': '/fixture/repo-b', 'project_id': 'obsidian-system'}]
        self.receipts = self.base/'receipts'
        self.config['observatory']['receipt_roots'] = [{'root_id': 'fixture_outcomes', 'producer': 'obsidian-agent', 'environment': 'personal', 'path': str(self.receipts)}]
        write_lines(root_path(self.config, 'wsl_claude')/f'a/{S1}.jsonl', claude_session(S1, '/fixture/repo-a', [('2026-09-15T10:00:00Z', 100)]))
        write_lines(root_path(self.config, 'wsl_claude')/f'b/{S2}.jsonl', claude_session(S2, '/fixture/repo-b', [('2026-09-15T10:00:00Z', 50)]))

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_outcomes_never_duplicate_session_cost_and_cross_project_stays_shared(self):
        write_lines(self.receipts/'a.jsonl', [receipt(1, 'outcome.started', 'a', 'private-project', session=S1),
            receipt(2, 'session.bound', 'a', 'private-project', session=S1), receipt(3, 'outcome.started', 'b', 'private-project', session=S1),
            receipt(4, 'session.bound', 'b', 'private-project', session=S1),
            receipt(5, 'session.bound', 'cross', 'private-project', session=S1), receipt(6, 'session.bound', 'cross', 'private-project', session=S2)])
        observatory.collect_observatory(self.config, PROJECT_ROOT, {}, NOW)
        con = observatory.connect_store(state_of(self.config)/observatory.STORE_NAME)
        public = outcomes.public_rows(con)[0]
        self.assertEqual(sum(r['project_attribution'] == 'shared' for r in public), 1)
        self.assertEqual(sum(r['project_attribution'] == 'correlated' for r in public), 2)
        self.assertTrue(all(r['api_equivalent_cost_usd'] is None for r in public))
        self.assertNotIn('private-project', json.dumps(public))
        self.assertNotIn(S1, json.dumps(public))
        con.close()

    def test_workspace_periods_attention_remainder_and_consumer_are_read_only(self):
        write_lines(self.receipts/'a.jsonl', [receipt(1, 'outcome.started', 'a', 'console-project'),
            receipt(2, 'outcome.disposition', 'a', 'console-project', disposition='satisfied')])
        observatory.collect_observatory(self.config, PROJECT_ROOT, {}, NOW)
        state = state_of(self.config)
        raw = {'schema_version': 1, 'event_id': str(__import__('uuid').uuid4()), 'project_id': 'obsidian-agent', 'mode': 'plan',
               'started_at': '2026-09-08T23:30:00Z', 'ended_at': '2026-09-09T00:30:00Z', 'status': 'completed'}
        write_lines(state/attention.LEDGER_FILE, [raw])
        os.chmod(state/attention.LEDGER_FILE, 0o600)
        reporting = {'interface': 'workspace-reporting-v1', 'repositories': [
            {'key': 'repo', 'groups': ['workspace'], 'checkouts': ['/fixture/repo-a']}],
            'projects': {'console-project': 'workspace'}, 'unclaimed': 'own-group'}
        path = state/observatory.STORE_NAME
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        view = consumer.consumer_view(PROJECT_ROOT, state, {'days': 7, 'reporting': reporting}, now=NOW)
        workspace = view['economics']['workspace']
        group = next(r for r in workspace['groups'] if r['key'] == 'workspace')
        self.assertEqual(group['attention']['recorded_seconds'], 1800)
        self.assertEqual(workspace['totals']['attention']['period']['recorded_seconds'], 1800)
        self.assertEqual(group['outcomes_period']['delivered'], 1)
        self.assertEqual(sum(r['period']['tokens'] for r in workspace['groups'])+workspace['shared']['period']['tokens'], workspace['totals']['period']['tokens'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(view['economics']['contract'], economics.CONTRACT)
        self.assertIsNone(view['economics']['report']['actual_cash']['amount_usd'])

    def test_full_revision_join_is_exact_and_conflicting_native_project_is_shared(self):
        revision = 'f'*40
        write_lines(self.receipts/'a.jsonl', [receipt(1, 'outcome.started', 'revision-exact', 'private-project', commit=revision),
            receipt(2, 'outcome.started', 'revision-conflict', 'private-project', commit=revision, session=S2)])
        observatory.collect_observatory(self.config, PROJECT_ROOT, {}, NOW)
        con = observatory.connect_store(state_of(self.config)/observatory.STORE_NAME)
        try:
            con.execute('INSERT INTO code_observations VALUES(?,?,?,?,?,?,?)', ('chg-fixture', 'obsidian-agent', revision, '2026-09-09T00:00:00Z', 1, 1, 0))
            con.commit()
            fields = economics.outcome_fields(con)
            self.assertEqual(fields['revision-exact']['project_id'], 'obsidian-agent')
            self.assertEqual(fields['revision-exact']['project_attribution'], 'exact')
            self.assertIsNone(fields['revision-conflict']['project_id'])
            self.assertEqual(fields['revision-conflict']['project_attribution'], 'shared')
            self.assertNotIn(revision, json.dumps(outcomes.public_rows(con)[0]))
        finally:
            con.close()

    def test_read_only_consumer_accepts_previous_store_schema(self):
        observatory.collect_observatory(self.config, PROJECT_ROOT, {}, NOW)
        state = state_of(self.config)
        path = state/observatory.STORE_NAME
        con = observatory.connect_store(path)
        con.execute('DROP TABLE code_roots')
        con.execute('DROP TABLE code_observations')
        con.execute('PRAGMA user_version=4')
        con.commit()
        con.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        view = consumer.consumer_view(PROJECT_ROOT, state, {'days': 7}, now=NOW)
        self.assertEqual(view['economics']['report']['coverage']['code']['status'], 'not-configured')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
