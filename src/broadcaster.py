import datetime
import json
import time
import signal
import sys
from typing import Optional

from db import get_connection, init_schema, JOURNAL_PATH
from node_adapter import broadcast_signed_payment, get_sender_nonce

BROADCAST_DELAY = float(1.0)

MAX_RETRY_BEFORE_REVIEW = 3

CONFIRMATION_DEPTH = 10

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    print(f"\n[broadcaster] Shutdown signal received. Finishing current operation...")
    _shutdown_requested = True


def run_broadcaster(batch_id: str, db_path: str = None):
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    conn = get_connection(db_path)
    init_schema(conn)

    batch = conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()

    if not batch:
        print(f"[broadcaster] ERROR: Batch '{batch_id}' not found in journal.")
        conn.close()
        return

    sender = batch["sender_public_key"]
    print(f"[broadcaster] Starting batch '{batch_id}' for sender {sender[:20]}...")

    entries = conn.execute(
        """SELECT * FROM entries
           WHERE batch_id = ? AND sender = ?
           ORDER BY nonce ASC""",
        (batch_id, sender),
    ).fetchall()

    if not entries:
        print("[broadcaster] No entries found for this batch.")
        conn.close()
        return

    total = len(entries)
    print(f"[broadcaster] Loaded {total} entries from journal.")

    RESOLVED_STATUSES = {"confirmation_depth_reached", "superseded", "failed_terminal"}
    BROADCAST_DONE_STATUSES = {"broadcast_accepted", "observed_on_best_chain",
                                "confirmation_depth_reached", "superseded"}

    first_unresolved_idx = None
    for i, e in enumerate(entries):
        if e["status"] not in RESOLVED_STATUSES:
            first_unresolved_idx = i
            break

    if first_unresolved_idx is None:
        print("[broadcaster] All entries are already resolved. Nothing to do.")
        conn.close()
        return

    resume_nonce = entries[first_unresolved_idx]["nonce"]
    print(f"[broadcaster] Resuming from nonce={resume_nonce} "
          f"(entry {first_unresolved_idx + 1}/{total})")

    for entry in entries[first_unresolved_idx:]:
        if _shutdown_requested:
            print("[broadcaster] Graceful shutdown: stopping before next entry.")
            break

        entry_id = entry["entry_id"]
        nonce = entry["nonce"]
        status = entry["status"]

        live = conn.execute(
            "SELECT status FROM entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        current_status = live["status"] if live else status

        print(f"\n[broadcaster] nonce={nonce} status={current_status}")

        if current_status in RESOLVED_STATUSES:
            print(f"  -> Already resolved ({current_status}). Skipping.")
            continue

        if current_status in BROADCAST_DONE_STATUSES:
            print(f"  -> Already broadcast ({current_status}). Running observation check.")
            _observe_entry(conn, entry_id, nonce, sender, batch_id)
            continue

        retry_count = _count_failed_attempts(conn, entry_id)
        if current_status == "needs_rebroadcast" and retry_count >= MAX_RETRY_BEFORE_REVIEW:
            _set_status(conn, entry_id, "needs_review")
            print(f"  -> Too many failures ({retry_count}). Marked needs_review.")
            _record_replacement_request(conn, entry_id, nonce, entry["fee"])
            print(f"  -> Replacement request recorded for nonce={nonce}.")
            # Lane is blocked — stop processing further nonces from this sender
            print(f"  !! Lane blocked at nonce={nonce}. Halting further broadcasts.")
            break

        accepted, message, node_response = broadcast_signed_payment(
            entry_id, nonce, entry["signed_payload"]
        )
        _record_attempt(conn, entry_id, accepted, message, node_response)

        if accepted:
            _set_status(conn, entry_id, "broadcast_accepted")
            print(f"  -> Broadcast accepted. {message}")
            # Immediately run observation check
            time.sleep(BROADCAST_DELAY)
            _observe_entry(conn, entry_id, nonce, sender, batch_id)
        else:
            _set_status(conn, entry_id, "needs_rebroadcast")
            print(f"  -> Broadcast failed: {message}. Marked needs_rebroadcast.")

        if _shutdown_requested:
            break

        time.sleep(BROADCAST_DELAY)

    summary = _get_summary(conn, batch_id)
    print(f"\n[broadcaster] Run complete.")
    print(f"  Resolved   : {summary['resolved']}")
    print(f"  Unresolved : {summary['unresolved']}")
    print(f"  Needs review: {summary['needs_review']}")

    conn.close()


def _observe_entry(conn, entry_id: str, nonce: int, sender: str, batch_id: str):
    import os
    MOCK_MODE = os.environ.get("MINA_NODE_MOCK", "1") == "1"

    now = datetime.datetime.utcnow().isoformat()

    if MOCK_MODE:
        conn.execute("BEGIN")
        conn.execute(
            """INSERT INTO chain_observations
               (entry_id, observed_at, block_height, block_hash, observation_type)
               VALUES (?, ?, ?, ?, 'best_chain')""",
            (entry_id, now, 100 + nonce, f"MockBlock{nonce:05d}", )
        )
        _set_status(conn, entry_id, "confirmation_depth_reached", conn_active=True)
        conn.execute("COMMIT")
        print(f"  -> [MOCK] Observed on chain. Status: confirmation_depth_reached")
    else:
        _set_status(conn, entry_id, "needs_review")
        print(f"  -> Not yet observed on chain. Marked needs_review for operator.")


def _set_status(conn, entry_id: str, new_status: str, conn_active: bool = False):
    if not conn_active:
        conn.execute("BEGIN")
    conn.execute(
        "UPDATE entries SET status = ? WHERE entry_id = ?",
        (new_status, entry_id)
    )
    if not conn_active:
        conn.execute("COMMIT")


def _record_attempt(conn, entry_id: str, accepted: bool, message: str, node_response):
    now = datetime.datetime.utcnow().isoformat()
    result = "accepted" if accepted else "rejected"
    response_str = json.dumps(node_response) if node_response else None
    conn.execute("BEGIN")
    conn.execute(
        """INSERT INTO broadcast_attempts (entry_id, attempted_at, result, node_response)
           VALUES (?, ?, ?, ?)""",
        (entry_id, now, result, response_str)
    )
    conn.execute("COMMIT")


def _count_failed_attempts(conn, entry_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM broadcast_attempts WHERE entry_id = ? AND result != 'accepted'",
        (entry_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def _record_replacement_request(conn, entry_id: str, nonce: int, current_fee: str):
    now = datetime.datetime.utcnow().isoformat()
    try:
        rec_fee = str(int(float(current_fee) * 1.5))  # recommend 50% fee bump
    except Exception:
        rec_fee = current_fee
    conn.execute("BEGIN")
    conn.execute(
        """INSERT INTO replacement_requests
           (entry_id, blocked_nonce, current_fee, recommended_fee, created_at, resolved)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (entry_id, nonce, current_fee, rec_fee, now)
    )
    conn.execute("COMMIT")


def _get_summary(conn, batch_id: str) -> dict:
    RESOLVED = ("confirmation_depth_reached", "superseded", "failed_terminal")
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM entries WHERE batch_id = ? GROUP BY status",
        (batch_id,)
    ).fetchall()
    resolved = 0
    unresolved = 0
    needs_review = 0
    for row in rows:
        if row["status"] in RESOLVED:
            resolved += row["cnt"]
        elif row["status"] == "needs_review":
            needs_review += row["cnt"]
        else:
            unresolved += row["cnt"]
    return {"resolved": resolved, "unresolved": unresolved, "needs_review": needs_review}
