# Mina Payout Recovery Journal — MVP

A durable recovery daemon for nonce-ordered MINA payout batches.

## What It Does

Once a payout batch is imported into the journal, the operator no longer depends on
a non-persistent mempool or ad-hoc file handling to resume that batch safely.

**Core guarantees (MVP):**
- Pre-signed payments are stored durably in a local SQLite journal before broadcast
- Entries are broadcast in **strict nonce order** — a blocked nonce halts the lane
- After a crash or restart, the tool **resumes from the first unresolved nonce**
- Ambiguous outcomes are surfaced as `needs_review`, not silently assumed successful
- A JSON settlement report is produced at the end of each batch

---

## Requirements

- Python 3.8+ (stdlib only — `sqlite3`, `hashlib`, `json` — no pip install needed)
- No database server, no external dependencies

---

## Quick Start

```bash
# Clone / copy the project
cd mina-journal

# 1. Import a payout batch
python3 src/mina_journal.py import-batch samples/sample_batch.json

# 2. Run the broadcaster (nonce-ordered, crash-safe)
python3 src/mina_journal.py run payout-2024-q4-batch-001

# 3. Check batch status
python3 src/mina_journal.py status payout-2024-q4-batch-001

# 4. Generate settlement report
python3 src/mina_journal.py report payout-2024-q4-batch-001 --output report.json
```

---

## Crash Recovery Demo

Run the full deterministic demo that proves all 7 acceptance criteria:

```bash
python3 demo_crash_recovery.py
```

This demo:
1. Imports a 5-entry batch
2. Proves duplicate import is rejected
3. Confirms nonce ordering
4. Simulates a process kill after 2 of 5 entries are broadcast
5. Restarts and proves resumption from nonce 102 (first unresolved)
6. Shows the status table
7. Generates and validates the settlement report

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MINA_JOURNAL_PATH` | `mina_journal.db` | Path to SQLite journal file |
| `MINA_NODE_MOCK` | `1` | `1` = mock mode (no node needed), `0` = real node |
| `MINA_NODE_URL` | `http://localhost:3085/graphql` | Private Mina node GraphQL endpoint |
| `MINA_MOCK_FAIL_AT_NONCE` | _(unset)_ | Simulate broadcast failure at this nonce |

**Default mode is mock** — fully functional without a live node. Set `MINA_NODE_MOCK=0`
and `MINA_NODE_URL` to connect to your private node.

---

## CLI Reference

### `import-batch <batch.json>`

Reads a JSON batch manifest and writes entries into the journal.

- Duplicate `batch_id` is **rejected** (idempotent)
- Duplicate `entry_id` within different batches is silently skipped
- Nonce duplicates within a batch are rejected with an error

```bash
python3 src/mina_journal.py import-batch samples/sample_batch.json
```

### `run <batch_id>`

Broadcasts entries in strict nonce order. Resumable after any crash.

- Loads journal state on every startup — mempool state is irrelevant
- If nonce N is unresolved, nonce N+1 is **never sent**
- On broadcast failure after `MAX_RETRY_BEFORE_REVIEW` attempts, marks entry
  `needs_review` and emits a replacement request

```bash
python3 src/mina_journal.py run payout-2024-q4-batch-001
```

### `status <batch_id>`

Prints a status table with per-entry nonce, status, and receiver.

```bash
python3 src/mina_journal.py status payout-2024-q4-batch-001
```

### `report <batch_id> [--output file.json]`

Generates a JSON settlement report. Saved to the journal and optionally to a file.

```bash
python3 src/mina_journal.py report payout-2024-q4-batch-001 --output settlement.json
```

---

## Batch JSON Schema

```json
{
  "batch_id": "string (unique per batch)",
  "network_id": "mainnet | devnet | testnet",
  "sender_public_key": "B62q...",
  "payments": [
    {
      "nonce": 100,
      "receiver": "B62q...",
      "amount": "100000000000",
      "fee": "1000000",
      "memo": "optional memo",
      "valid_until": "4294967295",
      "signed_payload": "base64-or-hex signed tx",
      "external_id": "optional-your-id"
    }
  ]
}
```

`signed_payload` is the pre-signed transaction produced by your existing signing
workflow (`mina-signer` or equivalent). **The daemon never holds private keys.**

---

## Entry State Machine

```
imported
  └─► broadcast_accepted
        └─► observed_on_best_chain
              └─► confirmation_depth_reached  ✅ (terminal)
  └─► needs_rebroadcast (retry)
        └─► needs_review ⚠️  (after max retries)
              └─► [operator imports replacement]
                    └─► superseded ⬛ (original)
                    └─► confirmation_depth_reached ✅ (replacement)
  └─► failed_terminal ❌ (explicit terminal failure)
```

If the daemon cannot prove a clean outcome, it marks `needs_review` rather than
guessing. **This is intentional.** The tool reduces ambiguity, it does not hide it.

---

## Architecture

```
mina-journal/
├── src/
│   ├── mina_journal.py    # CLI entrypoint (import-batch, run, status, report)
│   ├── db.py              # SQLite journal schema + WAL mode connection
│   ├── importer.py        # Batch importer, entry_id derivation, dedup
│   ├── broadcaster.py     # Nonce-ordered broadcast worker + state machine
│   ├── node_adapter.py    # Mina node GraphQL adapter (mock + real)
│   └── reporter.py        # Status display + JSON settlement report
├── samples/
│   └── sample_batch.json  # Example batch manifest
├── demo_crash_recovery.py # Full acceptance test / crash-recovery demo
└── README.md
```

### SQLite Schema (WAL mode)

| Table | Purpose |
|---|---|
| `batches` | One row per imported batch |
| `entries` | One row per payment, indexed by `(sender, nonce)` |
| `broadcast_attempts` | Append-only log of every send attempt |
| `chain_observations` | Best-chain sightings |
| `replacement_requests` | Blocked-nonce replacement plans |
| `reports` | Saved settlement reports |

---

## Security Model

- The daemon **never holds private keys**
- Signing is external — import pre-signed payments only
- The broadcaster is intended for use against a **private Mina node GraphQL endpoint**
- The tool is not designed around a public-facing node

---

## MVP Scope

**Included:**
- One sender account per batch
- Generic JSON batch import
- Local SQLite journal (WAL mode)
- `import-batch`, `run`, `status`, `report` CLI commands
- Crash-safe restart recovery
- Conservative chain observation (mock + real node)
- JSON settlement report

**Excluded (V1 full scope, not MVP):**
- Payout reward calculation
- Payout-script reference adapter
- Private-key custody
- Automatic fee bumping / replacement transaction ingestion
- Multi-sender orchestration
- CSV export
- Docker packaging
- Hosted service / SaaS

---

## Assumptions Made

1. **Signed payload is opaque** — the daemon treats `signed_payload` as a string and
   passes it directly to the node. Format (base64, hex, JSON) matches what your node accepts.
2. **One sender per batch** — the MVP enforces one nonce lane. Multi-sender is V1 scope.
3. **Nonces are contiguous integers** — gaps are allowed but the lane halts at the first
   unresolved nonce, so gaps block forward progress until filled.
4. **Mock mode** uses a fake block hash and incremental block height for chain observations.
   Real mode requires a private Mina node with GraphQL enabled.
