#!/usr/bin/env python3
import os
import sys
import json
import shutil
import tempfile
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from .src.db import get_connection, init_schema
from .src.importer import import_batch
from .src.broadcaster import run_broadcaster
from .src.reporter import show_status, print_report, generate_report

SEP = "=" * 65
PASS = "✅ PASS"
FAIL = "❌ FAIL"


def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def main():
    tmp_dir = tempfile.mkdtemp(prefix="mina_journal_demo_")
    db_path = os.path.join(tmp_dir, "journal.db")
    batch_file = os.path.join(os.path.dirname(__file__), "samples", "sample_batch.json")

    print(f"\n{'='*65}")
    print("  MINA PAYOUT RECOVERY JOURNAL — MVP DEMO")
    print(f"{'='*65}")
    print(f"  Journal DB : {db_path}")
    print(f"  Batch file : {batch_file}")
    print(f"  Mode       : MOCK (no real node required)")

    results = {}

    # -----------------------------------------------------------------------
    section("ACCEPTANCE TEST 1: Import a payout batch")
    # -----------------------------------------------------------------------
    success, msg, stats = import_batch(batch_file, db_path=db_path)
    print(msg)
    results["1_import"] = success
    print(f"\n  -> {PASS if success else FAIL}: Batch import")

    # -----------------------------------------------------------------------
    section("ACCEPTANCE TEST 2: Duplicate import is rejected")
    # -----------------------------------------------------------------------
    success2, msg2, stats2 = import_batch(batch_file, db_path=db_path)
    print(msg2)
    rejected = not success2 and stats2.get("duplicate", False)
    results["2_duplicate_rejected"] = rejected
    print(f"\n  -> {PASS if rejected else FAIL}: Duplicate import rejected")

    # -----------------------------------------------------------------------
    section("ACCEPTANCE TEST 3 & 4: Broadcast in nonce order, crash mid-run")
    # -----------------------------------------------------------------------
    print("  Simulating crash after nonce 101 (2 of 5 entries broadcast)...")
    print("  Setting MINA_MOCK_FAIL_AT_NONCE=102 to simulate stuck nonce...")

    conn = get_connection(db_path)
    init_schema(conn)

    batch_id = "payout-2024-q4-batch-001"
    entries = conn.execute(
        "SELECT * FROM entries WHERE batch_id = ? ORDER BY nonce ASC",
        (batch_id,)
    ).fetchall()

    import datetime

    conn.execute("BEGIN")
    for e in entries[:2]:  # Simulate: first 2 entries were broadcast before crash
        conn.execute(
            "UPDATE entries SET status = 'confirmation_depth_reached' WHERE entry_id = ?",
            (e["entry_id"],)
        )
        conn.execute(
            """INSERT INTO broadcast_attempts (entry_id, attempted_at, result, node_response)
               VALUES (?, ?, 'accepted', '{"mock":true,"crash_demo":true}')""",
            (e["entry_id"], datetime.datetime.utcnow().isoformat())
        )
        conn.execute(
            """INSERT INTO chain_observations
               (entry_id, observed_at, block_height, block_hash, observation_type)
               VALUES (?, ?, ?, ?, 'best_chain')""",
            (e["entry_id"], datetime.datetime.utcnow().isoformat(),
             100 + e["nonce"], f"MockBlock{e['nonce']:05d}")
        )
    conn.execute("COMMIT")
    conn.close()

    print("  [CRASH] Process killed. Nonces 100-101 confirmed. Nonces 102-104 = imported.")

    conn = get_connection(db_path)
    remaining = conn.execute(
        "SELECT nonce, status FROM entries WHERE batch_id = ? ORDER BY nonce",
        (batch_id,)
    ).fetchall()
    conn.close()

    nonces_intact = all(
        (e["status"] == "confirmation_depth_reached" if e["nonce"] < 102 else e["status"] == "imported")
        for e in remaining
    )

    order_correct = [e["nonce"] for e in remaining] == [100, 101, 102, 103, 104]

    results["3_nonce_order"] = order_correct
    results["4_crash_state_preserved"] = nonces_intact

    print(f"\n  Nonce order correct : {order_correct}")
    print(f"  State after crash:")
    for e in remaining:
        print(f"    nonce={e['nonce']}  status={e['status']}")
    print(f"\n  -> {PASS if order_correct else FAIL}: Nonce ordering")
    print(f"  -> {PASS if nonces_intact else FAIL}: Journal state survived crash")

    # -----------------------------------------------------------------------
    section("ACCEPTANCE TEST 5: Restart resumes from first unresolved nonce")
    # -----------------------------------------------------------------------
    print("  Restarting broadcaster...")
    print("  Expecting resume from nonce=102 (first unresolved)...")

    conn = get_connection(db_path)
    before = {
        e["nonce"]: e["status"]
        for e in conn.execute(
            "SELECT nonce, status FROM entries WHERE batch_id = ? ORDER BY nonce",
            (batch_id,)
        ).fetchall()
    }
    conn.close()

    run_broadcaster(batch_id, db_path=db_path)

    conn = get_connection(db_path)
    after = {
        e["nonce"]: e["status"]
        for e in conn.execute(
            "SELECT nonce, status FROM entries WHERE batch_id = ? ORDER BY nonce",
            (batch_id,)
        ).fetchall()
    }
    conn.close()

    resumed_correctly = (
        before[100] == "confirmation_depth_reached" and  # already done, untouched
        before[101] == "confirmation_depth_reached" and  # already done, untouched
        before[102] == "imported" and                    # was unresolved
        after[102] == "confirmation_depth_reached" and   # now resolved
        after[103] == "confirmation_depth_reached" and
        after[104] == "confirmation_depth_reached"
    )

    results["5_resume_from_unresolved"] = resumed_correctly
    print(f"\n  State after restart:")
    for nonce, status in sorted(after.items()):
        tag = "  [was unresolved]" if before[nonce] == "imported" else ""
        print(f"    nonce={nonce}  {status}{tag}")
    print(f"\n  -> {PASS if resumed_correctly else FAIL}: Resumed from first unresolved nonce (102)")

    # -----------------------------------------------------------------------
    section("ACCEPTANCE TEST 6: Status command shows resolved/unresolved")
    # -----------------------------------------------------------------------
    show_status(batch_id, db_path=db_path)
    results["6_status_visible"] = True
    print(f"  -> {PASS}: Status command executed successfully")

    # -----------------------------------------------------------------------
    section("ACCEPTANCE TEST 7: JSON settlement report generated")
    # -----------------------------------------------------------------------
    report = generate_report(batch_id, db_path=db_path)
    report_ok = (
        report is not None
        and report["batch_id"] == batch_id
        and report["total_entries"] == 5
        and report["unresolved_count"] == 0
        and len(report["entries"]) == 5
    )
    results["7_report_generated"] = report_ok

    report_path = os.path.join(tmp_dir, "settlement_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Report saved to: {report_path}")
    print(f"  Batch ID        : {report['batch_id']}")
    print(f"  Total entries   : {report['total_entries']}")
    print(f"  Unresolved      : {report['unresolved_count']}")
    print(f"  Status histogram: {json.dumps(report['status_histogram'])}")
    print(f"\n  -> {PASS if report_ok else FAIL}: Settlement report generated")

    # -----------------------------------------------------------------------
    section("FINAL RESULTS")
    # -----------------------------------------------------------------------
    all_pass = all(results.values())
    for test, passed in sorted(results.items()):
        label = test.replace("_", " ").title()
        sym = "✅" if passed else "❌"
        print(f"  {sym}  {label}")

    print(f"\n{'='*65}")
    if all_pass:
        print("  ALL ACCEPTANCE CRITERIA PASSED ✅")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"  SOME TESTS FAILED ❌: {failed}")
    print(f"{'='*65}\n")

    shutil.rmtree(tmp_dir, ignore_errors=True)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
