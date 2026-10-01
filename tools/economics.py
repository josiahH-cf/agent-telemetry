#!/usr/bin/env python3
"""Append restricted dated project context or cash evidence from a local JSON file."""
from pathlib import Path
import argparse
import datetime as dt
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import economics
from tools import attention


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-file', required=True, type=Path)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--state-root', type=Path, default=attention.default_state_root())
    args = parser.parse_args()
    try:
        if args.record_file.stat().st_size > attention.MAX_LEDGER_LINE_BYTES:
            raise ValueError('economics_record_oversized')
        raw = json.loads(args.record_file.read_text(encoding='utf-8'))
        result = economics.append_record(args.project_root, args.state_root, raw, dt.datetime.now(dt.timezone.utc))
    except attention.AttentionError as error:
        print(error.reason)
        return error.exit_code
    except (OSError, ValueError, TypeError, KeyError):
        print('economics_record_rejected')
        return 1
    print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
