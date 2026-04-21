import os
import json
import urllib.request
import urllib.error
from typing import Tuple, Optional, Dict, Any


MOCK_MODE = os.environ.get("MINA_NODE_MOCK", "1") == "1"
NODE_URL = os.environ.get("MINA_NODE_URL", "http://localhost:3085/graphql")

MOCK_FAIL_AT_NONCE = os.environ.get("MINA_MOCK_FAIL_AT_NONCE", None)


SEND_PAYMENT_MUTATION = """
mutation SendRosettaTransaction($input: SendRosettaTransactionInput!) {
  sendRosettaTransaction(input: $input) {
    payment {
      id
      nonce
      hash
    }
  }
}
"""


def broadcast_signed_payment(
    entry_id: str,
    nonce: int,
    signed_payload: str,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    if MOCK_MODE:
        return _mock_broadcast(nonce, signed_payload)
    else:
        return _real_broadcast(signed_payload)


def _mock_broadcast(nonce: int, signed_payload: str) -> Tuple[bool, str, Optional[Dict]]:
    if MOCK_FAIL_AT_NONCE is not None and int(MOCK_FAIL_AT_NONCE) == nonce:
        return False, f"[MOCK] Node rejected payment at nonce {nonce} (simulated failure)", {
            "mock": True,
            "nonce": nonce,
            "accepted": False,
            "reason": "simulated_rejection",
        }

    fake_hash = f"CkpaMock{nonce:05d}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    response = {
        "mock": True,
        "nonce": nonce,
        "accepted": True,
        "hash": fake_hash,
    }
    return True, f"[MOCK] Accepted nonce={nonce} hash={fake_hash}", response


def _real_broadcast(signed_payload: str) -> Tuple[bool, str, Optional[Dict]]:
    try:
        payload = json.dumps({
            "query": SEND_PAYMENT_MUTATION,
            "variables": {
                "input": {"signed_rosetta_transaction": signed_payload}
            }
        }).encode("utf-8")

        req = urllib.request.Request(
            NODE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        if "errors" in body:
            return False, f"Node returned errors: {body['errors']}", body

        payment = body.get("data", {}).get("sendRosettaTransaction", {}).get("payment")
        if payment:
            return True, f"Accepted nonce={payment.get('nonce')} hash={payment.get('hash')}", body
        else:
            return False, f"Unexpected node response: {body}", body

    except urllib.error.URLError as e:
        return False, f"Network error contacting node: {e}", None
    except Exception as e:
        return False, f"Broadcast error: {e}", None


def get_sender_nonce(sender: str) -> Optional[int]:
    if MOCK_MODE:
        return None

    query = """
    query AccountNonce($publicKey: PublicKey!) {
      account(publicKey: $publicKey) {
        nonce
        inferredNonce
      }
    }
    """
    try:
        payload = json.dumps({
            "query": query,
            "variables": {"publicKey": sender}
        }).encode("utf-8")
        req = urllib.request.Request(
            NODE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        account = body.get("data", {}).get("account", {})
        nonce = account.get("inferredNonce") or account.get("nonce")
        return int(nonce) if nonce is not None else None
    except Exception:
        return None
