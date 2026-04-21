#!/usr/bin/env python3
import sys
import os
import argparse
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_connection, init_schema, JOURNAL_PATH
from importer import import_batch
from broadcaster import run_broadcaster
from reporter import show_status, print_report


def cmd_import(args):
    print(f"[import-batch] Reading: {args.batch_file}")
    success, message, stats = import_batch(args.batch_file, db_path=args.db)
    print(message)
    sys.exit(0 if success else 1)


def cmd_run(args):
    print(f"[run] Starting broadcaster for batch: {args.batch_id}")
    mock = os.environ.get("MINA_NODE_MOCK", "1")
    if mock == "1":
        print("[run] Mode: MOCK (set MINA_NODE_MOCK=0 and MINA_NODE_URL for real node)")
    else:
        url = os.environ.get("MINA_NODE_URL", "http://localhost:3085/graphql")
        print(f"[run] Mode: REAL node at {url}")
    run_broadcaster(args.batch_id, db_path=args.db)


def cmd_status(args):
    show_status(args.batch_id, db_path=args.db)


def cmd_report(args):
    output = getattr(args, "output", None)
    print_report(args.batch_id, output_path=output, db_path=args.db)


def main():
    parser = argparse.ArgumentParser(
        prog="mina_journal",
        description="Mina Payout Recovery Journal — MVP CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--db",
        default=None,
        help=f"Path to SQLite journal (default: {JOURNAL_PATH})"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_import = subparsers.add_parser(
        "import-batch",
        help="Import a JSON payout batch into the journal"
    )
    p_import.add_argument("batch_file", help="Path to the batch JSON file")
    p_import.set_defaults(func=cmd_import)

    p_run = subparsers.add_parser(
        "run",
        help="Broadcast entries in nonce order (resumable after crash)"
    )
    p_run.add_argument("batch_id", help="Batch ID to run")
    p_run.set_defaults(func=cmd_run)

    p_status = subparsers.add_parser(
        "status",
        help="Show current status of a batch"
    )
    p_status.add_argument("batch_id", help="Batch ID to inspect")
    p_status.set_defaults(func=cmd_status)

    p_report = subparsers.add_parser(
        "report",
        help="Generate a JSON settlement report"
    )
    p_report.add_argument("batch_id", help="Batch ID to report on")
    p_report.add_argument("--output", "-o", default=None,
                          help="Save report to this file path")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()

    conn = get_connection(args.db)
    init_schema(conn)
    conn.close()

    args.func(args)


if __name__ == "__main__":
    main()
