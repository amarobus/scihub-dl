"""Exception hierarchy for scihub_dl."""


class ScihubError(Exception):
    """Base class for all scihub_dl errors."""


class MirrorUnreachableError(ScihubError):
    """No configured mirror could be reached."""


class ChallengeError(ScihubError):
    """The Altcha proof-of-work challenge could not be fetched or solved."""


class NotFoundError(ScihubError):
    """Sci-Hub has no PDF for the requested DOI."""


class InvalidDOIError(ScihubError):
    """The supplied string does not look like a DOI."""


class DownloadError(ScihubError):
    """The PDF could not be downloaded or was not a valid PDF."""
