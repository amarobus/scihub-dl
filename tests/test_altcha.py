import base64
import hashlib
import json

import pytest

from scihub_dl import altcha
from scihub_dl.errors import ChallengeError

from .conftest import make_challenge


class TestSolveChallenge:
    def test_finds_correct_number(self):
        chal, expected = make_challenge(number=1234)
        assert altcha.solve_challenge(chal) == expected

    def test_solves_zero(self):
        chal, expected = make_challenge(number=0)
        assert altcha.solve_challenge(chal) == 0

    @pytest.mark.parametrize("algorithm", ["SHA-256", "SHA-384", "SHA-512"])
    def test_supported_algorithms(self, algorithm):
        salt = "s?expires=1&"
        number = 99
        fn = {"SHA-256": hashlib.sha256, "SHA-384": hashlib.sha384, "SHA-512": hashlib.sha512}[
            algorithm
        ]
        chal = {
            "algorithm": algorithm,
            "challenge": fn((salt + str(number)).encode()).hexdigest(),
            "maxNumber": 1000,
            "salt": salt,
            "signature": "sig",
        }
        assert altcha.solve_challenge(chal) == number

    def test_unsupported_algorithm(self):
        chal, _ = make_challenge()
        chal["algorithm"] = "MD5"
        with pytest.raises(ChallengeError, match="Unsupported"):
            altcha.solve_challenge(chal)

    def test_malformed_challenge(self):
        with pytest.raises(ChallengeError, match="Malformed"):
            altcha.solve_challenge({"algorithm": "SHA-256"})

    def test_no_solution_in_range(self):
        chal, _ = make_challenge(number=9999)
        chal["challenge"] = "f" * 64  # unreachable digest
        with pytest.raises(ChallengeError, match="No Altcha solution"):
            altcha.solve_challenge(chal)

    def test_max_iterations_caps_search(self):
        chal, _ = make_challenge(number=5000)
        with pytest.raises(ChallengeError):
            altcha.solve_challenge(chal, max_iterations=10)


class TestBuildPayload:
    def test_payload_roundtrip(self):
        chal, number = make_challenge(number=777)
        payload_b64 = altcha.build_payload(chal, number)
        decoded = json.loads(base64.b64decode(payload_b64))
        assert decoded["number"] == 777
        assert decoded["challenge"] == chal["challenge"]
        assert decoded["salt"] == chal["salt"]
        assert decoded["signature"] == chal["signature"]
        assert decoded["algorithm"] == "SHA-256"

    def test_solve_returns_b64_payload(self):
        chal, number = make_challenge(number=321)
        payload_b64 = altcha.solve(chal)
        decoded = json.loads(base64.b64decode(payload_b64))
        assert decoded["number"] == number
        # verify the echoed solution actually satisfies the challenge
        recomputed = hashlib.sha256(
            (decoded["salt"] + str(decoded["number"])).encode()
        ).hexdigest()
        assert recomputed == chal["challenge"]
