class PlannerError(Exception):
    """An expected, user-facing failure (bad input, no route, upstream down)."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message, *, status_code=None, code=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class GeocodingError(PlannerError):
    status_code = 400
    code = "location_not_found"


class RoutingError(PlannerError):
    status_code = 422
    code = "no_route"


class UpstreamError(PlannerError):
    """The free routing/geocoding service failed or timed out."""

    status_code = 502
    code = "upstream_unavailable"
