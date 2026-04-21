import json
import hashlib
import datetime
from typing import Dict, Any, List, Tuple
import sqlite3

from db import get_connection, init_schema, JOURNAL_PATH


def derive_entry_id(sender: str, nonce: int, receiver: str, amount: str,
                    fee: str, memo: str, network_id: str, signed_payload: str) -> str:
    canonical = "|".join([
        sender.strip(),
        str(nonce),
        receiver.strip(),
        str(amount).strip(),
        str(fee).strip(),
        (memo or "").strip(),
        network_id.strip(),
        signed_payload.strip(),
    ])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_batch_manifest(manifest: Dict[str, Any]) -> List[str]:
    errors = []
    for field in ["batch_id", "network_id", "sender_public_key", "payments"]:
        if field not in manifest:
            errors.append(f"Missing required field: {field}")
    if errors:
        return errors
    if not isinstance(manifest["payments"], list) or len(manifest["payments"]) == 0:
        errors.append("'payments' must be a non-empty list")
        return errors
    for i, p in enumerate(manifest["payments"]):
        for pf in ["nonce", "receiver", "amount", "fee", "signed_payload"]:
            if pf not in p:
                errors.append(f"Payment[{i}] missing required field: {pf}")
    return errors


def import_batch(json_path: str, db_path: str = None) -> Tuple[bool, str, Dict]:
    try:
        with open(json_path, "r") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        return False, f"File not found: {json_path}", {}
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}", {}

    errors = validate_batch_manifest(manifest)
    if errors:
        return False, "Validation failed:\n  " + "\n  ".join(errors), {}

    batch_id = manifest["batch_id"]
    network_id = manifest["network_id"]
    sender = manifest["sender_public_key"]
    payments = manifest["payments"]

    conn = get_connection(db_path)
    init_schema(conn)

    now = datetime.datetime.utcnow().isoformat()

    stats = {
        "batch_id": batch_id,
        "total": len(payments),
        "imported": 0,
        "skipped_duplicate": 0,
        "errors": [],
    }

    try:
        conn.execute("BEGIN")

        existing_batch = conn.execute(
            "SELECT batch_id FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()

        if existing_batch:
            conn.execute("ROLLBACK")
            return False, (
                f"Batch '{batch_id}' already exists in journal. "
                "Import is idempotent — re-importing the same batch_id is rejected. "
                "Use a new batch_id for a new batch."
            ), {"batch_id": batch_id, "duplicate": True}

        nonces_seen = {}
        for i, p in enumerate(payments):
            n = p["nonce"]
            if n in nonces_seen:
                conn.execute("ROLLBACK")
                return False, (
                    f"Duplicate nonce {n} in payments[{nonces_seen[n]}] and payments[{i}]. "
                    "Each nonce must be unique within a batch."
                ), {}
            nonces_seen[n] = i

        conn.execute(
            """INSERT INTO batches (batch_id, network_id, sender_public_key, imported_at, total_entries, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            (batch_id, network_id, sender, now, len(payments)),
        )

        for p in payments:
            nonce = int(p["nonce"])
            receiver = p["receiver"]
            amount = str(p["amount"])
            fee = str(p["fee"])
            memo = p.get("memo", "")
            valid_until = p.get("valid_until", "")
            signed_payload = p["signed_payload"]
            external_id = p.get("external_id", None)

            entry_id = derive_entry_id(
                sender, nonce, receiver, amount, fee, memo, network_id, signed_payload
            )

            existing_entry = conn.execute(
                "SELECT entry_id FROM entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()

            if existing_entry:
                stats["skipped_duplicate"] += 1
                continue

            conn.execute(
                """INSERT INTO entries
                   (entry_id, batch_id, sender, nonce, receiver, amount, fee,
                    memo, valid_until, signed_payload, external_id, imported_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported')""",
                (entry_id, batch_id, sender, nonce, receiver, amount, fee,
                 memo, valid_until, signed_payload, external_id, now),
            )
            stats["imported"] += 1

        conn.execute("COMMIT")

    except Exception as e:
        conn.execute("ROLLBACK")
        return False, f"Database error during import: {e}", {}
    finally:
        conn.close()

    msg = (
        f"Batch '{batch_id}' imported successfully.\n"
        f"  Entries imported : {stats['imported']}\n"
        f"  Duplicates skipped: {stats['skipped_duplicate']}\n"
        f"  Total in manifest : {stats['total']}"
    )
    return True, msg, stats
