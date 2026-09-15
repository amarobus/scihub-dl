import hashlib
import json

import pytest

MIRROR = "https://sci-hub.ru"

PAPER_HTML = """
<!DOCTYPE html>
<html><head>
<title>Sci-Hub. Nanometre-scale thermometry in a living cell / Nature, 2013</title>
<meta name="citation_title" content="Nanometre-scale thermometry in a living cell">
<meta name="citation_author" content="Kucsko, G.">
<meta name="citation_author" content="Maurer, P. C.">
<meta name="citation_publication_date" content="2013">
<meta name="citation_journal_title" content="Nature">
<meta name="citation_doi" content="10.1038/nature12373">
<meta name="citation_pdf_url" content="//sci-hub.red/storage/moscow/2161/abc/kucsko2013.pdf">
</head><body>%s</body></html>
""" % ("padding " * 4000)

CAPTCHA_HTML = """
<!DOCTYPE html>
<html><head><title>Sci-Hub: robot check</title></head>
<body>
<altcha-widget challengeurl="/captcha/challenge/12345" hidelogo hidefooter></altcha-widget>
</body></html>
"""

NOT_FOUND_HTML = """
<!DOCTYPE html>
<html><head><title>Sci-Hub: article not found</title></head>
<body><p>Unfortunately, Sci-Hub doesn't have the requested document.</p></body></html>
"""

PDF_BYTES = b"%PDF-1.5\n" + b"0" * 2048 + b"\n%%EOF"


def make_challenge(salt="testsalt123?expires=9999999999&", number=4242, algorithm="SHA-256"):
    """Build a solvable Altcha challenge dict."""
    digest = hashlib.sha256((salt + str(number)).encode()).hexdigest()
    return {
        "algorithm": algorithm,
        "challenge": digest,
        "maxNumber": 10000,
        "salt": salt,
        "signature": "deadbeefsignature",
    }, number


@pytest.fixture
def mirror():
    return MIRROR


@pytest.fixture
def paper_html():
    return PAPER_HTML


@pytest.fixture
def captcha_html():
    return CAPTCHA_HTML


@pytest.fixture
def not_found_html():
    return NOT_FOUND_HTML


@pytest.fixture
def pdf_bytes():
    return PDF_BYTES


@pytest.fixture
def challenge():
    chal, number = make_challenge()
    return chal, number