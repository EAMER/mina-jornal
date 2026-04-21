import sqlite3
import os

JOURNAL_PATH = os.environ.get("MINA_JOURNAL_PATH", "mina_journal.db")


def get_connection(path: str = None) -> sqlite3.Connection:
    db_path = path or JOURNAL_PATH
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit off via explicit BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection):
    conn.executescript("""
    BEGIN;

    CREATE TABLE IF NOT EXISTS batches (
        batch_id        TEXT PRIMARY KEY,
        network_id      TEXT NOT NULL,
        sender_public_key TEXT NOT NULL,
        imported_at     TEXT NOT NULL,
        total_entries   INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'active'
    );

    CREATE TABLE IF NOT EXISTS entries (
        entry_id        TEXT PRIMARY KEY,
        batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
        sender          TEXT NOT NULL,
        nonce           INTEGER NOT NULL,
        receiver        TEXT NOT NULL,
        amount          TEXT NOT NULL,
        fee             TEXT NOT NULL,
        memo            TEXT,
        valid_until     TEXT,
        signed_payload  TEXT NOT NULL,
        external_id     TEXT,
        imported_at     TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'imported',
        UNIQUE(sender, nonce)
    );

    CREATE TABLE IF NOT EXISTS broadcast_attempts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id        TEXT NOT NULL REFERENCES entries(entry_id),
        attempted_at    TEXT NOT NULL,
        result          TEXT NOT NULL,
        node_response   TEXT
    );

    CREATE TABLE IF NOT EXISTS chain_observations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id        TEXT NOT NULL REFERENCES entries(entry_id),
        observed_at     TEXT NOT NULL,
        block_height    INTEGER,
        block_hash      TEXT,
        observation_type TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS replacement_requests (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id        TEXT NOT NULL REFERENCES entries(entry_id),
        blocked_nonce   INTEGER NOT NULL,
        current_fee     TEXT NOT NULL,
        recommended_fee TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        resolved        INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS reports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
        generated_at    TEXT NOT NULL,
        report_json     TEXT NOT NULL
    );

    COMMIT;
    """)
