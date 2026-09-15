"""Exception hierarchy for scihub_dl."""


class ScihubError(Exception):
    """Base class for all scihub_dl errors."""


class MirrorUnreachableError(ScihubError):
    """No configured mirror could be reached."""


class RedirectLoopError(ScihubError):
    """A mirror bounced the request in a redirect loop.

    Distinct from MirrorUnreachableError: the host *is* answering, it is just
    cycling the same URL (usually an anti-bot/cookie check that failed). This
    is mirror-specific and therefore retryable on a different mirror.
    """


class BotCheckError(ScihubError):
    """A mirror served an interstitial bot-check page we cannot clear.

    Not the same as Altcha (which we solve). Some mirrors gate behind a JS
    'Verification' page. Treat as retryable on another mirror, and never as
    'no PDF exists'.
    """


class ChallengeError(ScihubError):
    """The Altcha proof-of-work challenge could not be fetched or solved."""


class NotFoundError(ScihubError):
    """Sci-Hub affirmatively reports it has no PDF for the requested DOI.

    Only raised on positive evidence (an explicit not-found marker, or every
    mirror agreeing) - never merely because a PDF link could not be located.
    """


class ParseError(ScihubError):
    """The page loaded but no PDF link could be extracted from it.

    Deliberately distinct from NotFoundError: this means "we don't know",
    typically because a mirror changed its layout. Reporting it as
    "not found" would be a false negative.
    """


class InvalidDOIError(ScihubError):
    """The supplied string does not look like a DOI."""


class DownloadError(ScihubError):
    """The PDF could not be downloaded or was not a valid PDF."""