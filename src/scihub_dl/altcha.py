"""Altcha proof-of-work solver.

Sci-Hub gates page access with Altcha (https://altcha.org), an open-source
proof-of-work challenge. The server sends:

    {"algorithm": "SHA-256", "challenge": "<hex digest>",
     "maxNumber": 200000, "salt": "<salt>", "signature": "<hmac>"}

The client must find the integer ``n`` in ``[0, maxNumber]`` such that
``HASH(salt + str(n)) == challenge``, then echo the challenge back with the
discovered number as a base64-encoded JSON payload. This is pure computation -
no human interaction or image recognition is involved.
"""

import base64
import hashlib
import json
from typing import Any, Dict, Optional

from .errors import ChallengeError

_HASHES = {
    "SHA-256": hashlib.sha256,
    "SHA-384": hashlib.sha384,
    "SHA-512": hashlib.sha512,
}


def solve_challenge(challenge: Dict[str, Any], max_iterations: Optional[int] = None) -> int:
    """Brute-force the Altcha nonce. Returns the solution number.

    Raises ChallengeError if the challenge is malformed or has no solution
    within ``maxNumber``.
    """
    try:
        algorithm = challenge.get("algorithm", "SHA-256")
        salt = challenge["salt"]
        target = challenge["challenge"]
        max_number = int(challenge.get("maxNumber", 1_000_000))
    except (KeyError, TypeError, ValueError) as exc:
        raise ChallengeError(f"Malformed Altcha challenge: {challenge!r}") from exc

    hash_fn = _HASHES.get(algorithm.upper())
    if hash_fn is None:
        raise ChallengeError(f"Unsupported Altcha algorithm: {algorithm}")

    limit = max_number if max_iterations is None else min(max_number, max_iterations)

    salt_bytes = salt.encode()
    for n in range(limit + 1):
        if hash_fn(salt_bytes + str(n).encode()).hexdigest() == target:
            return n

    raise ChallengeError(
        f"No Altcha solution found within maxNumber={limit} (algorithm={algorithm})"
    )


def build_payload(challenge: Dict[str, Any], number: int) -> str:
    """Encode the solved challenge as the base64 JSON payload Altcha expects."""
    payload = {
        "algorithm": challenge.get("algorithm", "SHA-256"),
        "challenge": challenge["challenge"],
        "number": number,
        "salt": challenge["salt"],
        "signature": challenge["signature"],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def solve(challenge: Dict[str, Any], max_iterations: Optional[int] = None) -> str:
    """Solve a challenge and return the ready-to-POST base64 payload."""
    number = solve_challenge(challenge, max_iterations=max_iterations)
    return build_payload(challenge, number)
