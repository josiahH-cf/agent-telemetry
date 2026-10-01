"""Bounded read-only first-parent Git metadata; no code, filenames or prose."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sqlite3
from pathlib import Path

MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS code_roots (
 root_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, root_path TEXT NOT NULL,
 status TEXT NOT NULL, detail_code TEXT, last_scan_at TEXT, cursor_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_observations (
 change_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, native_revision TEXT NOT NULL,
 at TEXT NOT NULL, files_changed INTEGER, insertions INTEGER, deletions INTEGER,
 UNIQUE(project_id,native_revision)
);
"""
HEX = re.compile(r"[0-9a-f]{40,64}\Z")
HEADER = re.compile(r"([0-9a-f]{40,64}) ([0-9]+)(?: ([0-9a-f ]+))?\Z")
STAT = re.compile(r"\s*(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?\s*\Z")


class CodeError(ValueError):
    pass


def _git(path, *args, failure_reason='git_read_failed'):
    # Disable optional helpers. The only output formats requested
    # below contain hashes, timestamps, parent hashes and numeric shortstat.
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-C", str(path), *args], capture_output=True, timeout=20, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodeError("git_unavailable_or_timeout") from error
    if result.returncode or len(result.stdout) > 1_000_000:
        raise CodeError(failure_reason)
    try:
        return result.stdout.decode('ascii')
    except UnicodeDecodeError as error:
        raise CodeError("git_metadata_invalid") from error


def _parse(payload):
    records = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        header = HEADER.fullmatch(line.strip())
        if header:
            revision, epoch, parents = header.groups()
            parents = (parents or '').split()
            at = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).isoformat().replace('+00:00', 'Z')
            # Git omits shortstat for some merges. Missing is not zero.
            records.append({'revision': revision, 'at': at, 'parent': parents[0] if parents else None,
                            'files_changed': None if len(parents) > 1 else 0,
                            'insertions': None if len(parents) > 1 else 0,
                            'deletions': None if len(parents) > 1 else 0})
        elif records and (stat := STAT.fullmatch(line)):
            for key, value in zip(('files_changed', 'insertions', 'deletions'), stat.groups()):
                records[-1][key] = int(value or 0)
        else:
            raise CodeError('git_metadata_invalid')
    return records


def collect(connection, config, registry, salt, now):
    """Advance one bounded reconstructable cursor per explicitly configured root."""
    import observatory
    entries = (config.get('observatory') or {}).get('code_roots') or []
    results = []
    active_ids = set()
    rejections = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rid, pid = entry.get('root_id'), entry.get('project_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,39}', rid):
            rejections.append('code_root_identity_invalid')
            continue
        if rid in active_ids:
            rejections.append('code_root_identity_duplicate')
            continue
        active_ids.add(rid)
        path = Path(str(entry.get('path') or '')).expanduser()
        old = connection.execute('SELECT * FROM code_roots WHERE root_id=?', (rid,)).fetchone()
        cursor = json.loads(old['cursor_json']) if old else {}
        status, reason = 'current', None
        stamp = now.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        try:
            if pid not in registry['public'] or pid in observatory.BUCKET_IDS:
                raise CodeError('code_project_unregistered')
            if not entry.get('path') or not path.is_absolute():
                raise CodeError('code_root_not_explicit')
            resolved = observatory.resolve_project(str(path), '', registry, salt)
            if not resolved['registered'] or resolved['project_code'] != pid:
                raise CodeError('code_project_mapping_conflict')
            if old and (old['project_id'] != pid or old['root_path'] != str(path)):
                raise CodeError('code_source_identity_changed')
            if not path.is_dir():
                raise CodeError('code_root_unavailable')
            head = _git(path, 'rev-parse', '--verify', 'HEAD').strip()
            if not HEX.fullmatch(head):
                raise CodeError('git_metadata_invalid')
            for prior in (cursor.get('head'), cursor.get('pending_head')):
                if prior and _git(path, 'merge-base', prior, head, failure_reason='code_history_rewritten').strip() != prior:
                    raise CodeError('code_history_rewritten')
            raw_limit = entry.get('max_commits', 200)
            limit = min(1000, max(1, raw_limit)) if type(raw_limit) is int else 200
            since = entry.get('since', '1970-01-01')
            dt.date.fromisoformat(since)
            if cursor.get('since') and cursor['since'] != since:
                raise CodeError('code_coverage_configuration_changed')
            cursor.setdefault('since', since)
            if not cursor.get('pending_head') and head != cursor.get('head'):
                cursor.update(pending_head=head, pending_ref=head)
            if cursor.get('pending_head'):
                revs = [cursor['pending_ref']]
                if cursor.get('head'):
                    revs.append('^' + cursor['head'])
                rows = _parse(_git(path, 'log', '--first-parent', '--no-renames', '--no-ext-diff', '--no-textconv',
                                   '--format=%H %ct %P', '--shortstat', f'--max-count={limit+1}', f'--since={since}T00:00:00Z', *revs, '--'))
                chosen = rows[:limit]
                with connection:
                    for row in chosen:
                        key = 'chg-' + hashlib.sha256(f"{pid}\0{row['revision']}".encode()).hexdigest()[:24]
                        connection.execute('INSERT OR IGNORE INTO code_observations VALUES(?,?,?,?,?,?,?)',
                                           (key, pid, row['revision'], row['at'], row['files_changed'], row['insertions'], row['deletions']))
                    if len(rows) > limit:
                        cursor['pending_ref'] = chosen[-1]['parent']
                        status, reason = 'partial', 'bounded_backfill_pending'
                    else:
                        cursor['head'] = cursor.pop('pending_head')
                        cursor.pop('pending_ref', None)
                        cursor['since'] = since
            with connection:
                connection.execute('INSERT INTO code_roots VALUES(?,?,?,?,?,?,?) ON CONFLICT(root_id) DO UPDATE SET status=excluded.status,detail_code=excluded.detail_code,last_scan_at=excluded.last_scan_at,cursor_json=excluded.cursor_json',
                                   (rid, pid, str(path), status, reason, stamp, json.dumps(cursor, sort_keys=True)))
        except (CodeError, ValueError, OverflowError) as error:
            reason = str(error) if isinstance(error, CodeError) else 'code_configuration_invalid'
            status = 'retained-last-good' if old and (cursor.get('head') or old['status'] == 'partial') else 'unavailable'
            with connection:
                connection.execute('INSERT INTO code_roots VALUES(?,?,?,?,?,?,?) ON CONFLICT(root_id) DO UPDATE SET status=excluded.status,detail_code=excluded.detail_code,last_scan_at=excluded.last_scan_at',
                                   (rid, pid or '', str(path), status, reason, stamp, json.dumps(cursor, sort_keys=True)))
        results.append({'root_id': rid, 'status': status, 'detail_code': reason})
    with connection:
        for row in connection.execute('SELECT root_id FROM code_roots').fetchall():
            if row['root_id'] not in active_ids:
                connection.execute("UPDATE code_roots SET status='disabled',detail_code='source_disabled' WHERE root_id=?", (row['root_id'],))
        connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('code_adapter_rejections', json.dumps(sorted(set(rejections)))))
    return results


def public_rows(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_observations'").fetchone():
        return []
    return [{'change_id': r['change_id'], 'project_id': r['project_id'], 'at': r['at'],
             'date': r['at'][:10], 'files_changed': r['files_changed'], 'insertions': r['insertions'],
             'deletions': r['deletions'], 'source': 'read_only_git_first_parent'}
            for r in connection.execute('SELECT * FROM code_observations ORDER BY at,change_id')]


def coverage(connection):
    """Only safe counts/status enums enter the page; root identities stay private."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_roots'").fetchone():
        return {'status': 'not-configured', 'sources': 0, 'states': {}, 'from': None, 'to': None, 'by_project': {}, 'reasons': []}
    rows = list(connection.execute('SELECT project_id,status,detail_code,cursor_json,last_scan_at FROM code_roots'))
    counts = {state: sum(r['status'] == state for r in rows) for state in ('current', 'partial', 'retained-last-good', 'unavailable', 'disabled')}
    rejection_row = connection.execute("SELECT value FROM meta WHERE key='code_adapter_rejections'").fetchone()
    rejections = json.loads(rejection_row[0]) if rejection_row else []
    project_row = connection.execute("SELECT value FROM meta WHERE key='economics_projects'").fetchone()
    approved = set(json.loads(project_row[0])) if project_row else set()
    approved |= {r[0] for r in connection.execute('SELECT project_code FROM projects WHERE registered=1')}
    approved |= {r[0] for r in connection.execute('SELECT DISTINCT project_id FROM code_observations')}
    by_project = {}
    for pid in {r['project_id'] for r in rows} & approved:
        sources = [r for r in rows if r['project_id'] == pid]
        complete = all(r['status'] == 'current' for r in sources)
        by_project[pid] = {'status': 'current' if complete else 'partial',
                          'from': max((json.loads(r['cursor_json']).get('since', '9999-12-31') for r in sources), default=None) if complete else None}
    return {'status': 'partial' if rejections else 'not-configured' if not rows else 'current' if counts['current'] == len(rows) else 'partial',
            'sources': len(rows), 'states': counts,
            'from': max((json.loads(r['cursor_json']).get('since', '9999-12-31') for r in rows), default=None) if rows and counts['current'] == len(rows) else None,
            'to': min((r['last_scan_at'][:10] for r in rows if r['last_scan_at']), default=None) if rows and counts['current'] == len(rows) else None,
            'by_project': by_project,
            'reasons': sorted({r['detail_code'] for r in rows if r['detail_code']} | set(rejections))}


def preserve_rebuild_history(canonical, target):
    """Copy project-owned last-good metadata/cursors before bounded reconstruction."""
    if not canonical.is_file():
        return
    source = sqlite3.connect(f'file:{canonical}?mode=ro', uri=True)
    try:
        if source.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='code_observations'").fetchone()[0]:
            with target:
                target.executemany('INSERT OR IGNORE INTO code_observations VALUES(?,?,?,?,?,?,?)', source.execute('SELECT * FROM code_observations'))
                target.executemany('INSERT OR IGNORE INTO code_roots VALUES(?,?,?,?,?,?,?)', source.execute('SELECT * FROM code_roots'))
    finally:
        source.close()
