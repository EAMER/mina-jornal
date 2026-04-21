import json
import datetime
from typing import Optional

from db import get_connection, init_schema


STATUS_SYMBOLS = {
    "imported":                    "⬜",
    "broadcast_accepted":          "🟦",
    "observed_on_best_chain":      "🟨",
    "confirmation_depth_reached":  "✅",
    "needs_rebroadcast":           "🔄",
    "replacement_required":        "🔶",
    "superseded":                  "⬛",
    "needs_review":                "⚠️ ",
    "failed_terminal":             "❌",
}

TERMINAL_STATUSES = {"confirmation_depth_reached", "superseded", "failed_terminal"}


def show_status(batch_id: str, db_path: str = None):
    conn = get_connection(db_path)
    init_schema(conn)

    batch = conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()

    if not batch:
        print(f"Batch '{batch_id}' not found in journal.")
        conn.close()
        return

    entries = conn.execute(
        "SELECT * FROM entries WHERE batch_id = ? ORDER BY nonce ASC",
        (batch_id,)
    ).fetchall()

    replacements = conn.execute(
        """SELECT rr.*, e.entry_id
           FROM replacement_requests rr
           JOIN entries e ON e.entry_id = rr.entry_id
           WHERE e.batch_id = ?""",
        (batch_id,)
    ).fetchall()

    print(f"\n{'='*62}")
    print(f"  BATCH STATUS: {batch_id}")
    print(f"{'='*62}")
    print(f"  Sender  : {batch['sender_public_key'][:40]}...")
    print(f"  Network : {batch['network_id']}")
    print(f"  Imported: {batch['imported_at']}")
    print(f"  Entries : {batch['total_entries']}")
    print(f"{'-'*62}")
    print(f"  {'NONCE':>6}  {'STATUS':<30}  {'RECEIVER':<16}")
    print(f"{'-'*62}")

    counts = {k: 0 for k in STATUS_SYMBOLS}
    counts["unknown"] = 0

    for e in entries:
        sym = STATUS_SYMBOLS.get(e["status"], "?")
        receiver_short = e["receiver"][:16] + "..." if len(e["receiver"]) > 16 else e["receiver"]
        print(f"  {e['nonce']:>6}  {sym} {e['status']:<28}  {receiver_short}")
        counts[e["status"]] = counts.get(e["status"], 0) + 1

    print(f"{'-'*62}")

    resolved = sum(counts.get(s, 0) for s in TERMINAL_STATUSES)
    unresolved = len(entries) - resolved
    print(f"  Resolved: {resolved}  |  Unresolved: {unresolved}  |  Total: {len(entries)}")

    if replacements:
        print(f"\n  REPLACEMENT REQUESTS ({len(replacements)}):")
        for rr in replacements:
            status = "resolved" if rr["resolved"] else "OPEN"
            print(f"    nonce={rr['blocked_nonce']}  fee={rr['current_fee']} "
                  f"-> recommended={rr['recommended_fee']}  [{status}]")

    print(f"{'='*62}\n")
    conn.close()


def generate_report(batch_id: str, db_path: str = None, save: bool = True) -> Optional[dict]:
    conn = get_connection(db_path)
    init_schema(conn)

    batch = conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()

    if not batch:
        print(f"Batch '{batch_id}' not found in journal.")
        conn.close()
        return None

    entries = conn.execute(
        "SELECT * FROM entries WHERE batch_id = ? ORDER BY nonce ASC",
        (batch_id,)
    ).fetchall()

    attempts_by_entry = {}
    for row in conn.execute(
        """SELECT ba.* FROM broadcast_attempts ba
           JOIN entries e ON e.entry_id = ba.entry_id
           WHERE e.batch_id = ?""",
        (batch_id,)
    ).fetchall():
        attempts_by_entry.setdefault(row["entry_id"], []).append(dict(row))

    observations_by_entry = {}
    for row in conn.execute(
        """SELECT co.* FROM chain_observations co
           JOIN entries e ON e.entry_id = co.entry_id
           WHERE e.batch_id = ?""",
        (batch_id,)
    ).fetchall():
        observations_by_entry.setdefault(row["entry_id"], []).append(dict(row))

    replacements = conn.execute(
        """SELECT rr.* FROM replacement_requests rr
           JOIN entries e ON e.entry_id = rr.entry_id
           WHERE e.batch_id = ?""",
        (batch_id,)
    ).fetchall()

    entry_records = []
    unresolved = []
    for e in entries:
        rec = {
            "entry_id": e["entry_id"],
            "nonce": e["nonce"],
            "receiver": e["receiver"],
            "amount": e["amount"],
            "fee": e["fee"],
            "memo": e["memo"],
            "final_status": e["status"],
            "imported_at": e["imported_at"],
            "external_id": e["external_id"],
            "broadcast_attempts": attempts_by_entry.get(e["entry_id"], []),
            "chain_observations": observations_by_entry.get(e["entry_id"], []),
        }
        entry_records.append(rec)
        if e["status"] not in TERMINAL_STATUSES:
            unresolved.append({"nonce": e["nonce"], "status": e["status"], "entry_id": e["entry_id"]})

    histogram = {}
    for e in entries:
        histogram[e["status"]] = histogram.get(e["status"], 0) + 1

    report = {
        "report_version": "1.0",
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "batch_id": batch_id,
        "network_id": batch["network_id"],
        "sender": batch["sender_public_key"],
        "total_entries": batch["total_entries"],
        "status_histogram": histogram,
        "unresolved_entries": unresolved,
        "unresolved_count": len(unresolved),
        "replacement_history": [dict(r) for r in replacements],
        "entries": entry_records,
    }

    if save:
        now = datetime.datetime.utcnow().isoformat()
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO reports (batch_id, generated_at, report_json) VALUES (?, ?, ?)",
            (batch_id, now, json.dumps(report))
        )
        conn.execute("COMMIT")

    conn.close()
    return report


def print_report(batch_id: str, output_path: Optional[str] = None, db_path: str = None):
    report = generate_report(batch_id, db_path=db_path)
    if not report:
        return

    report_json = json.dumps(report, indent=2)

    if output_path:
        with open(output_path, "w") as f:
            f.write(report_json)
        print(f"Settlement report saved to: {output_path}")
    else:
        print(report_json)
