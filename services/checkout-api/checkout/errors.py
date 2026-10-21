"""Expected failures, each with its HTTP status. Anything else becomes a 500."""


class HttpError(Exception):
    status = 500


class BadRequest(HttpError):
    status = 400


class NotFound(HttpError):
    status = 404
