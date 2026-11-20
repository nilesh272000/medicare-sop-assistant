"""Domain errors. The API layer maps each to an HTTP status and a stable error code."""


class DomainError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class UnknownUserError(DomainError):
    code = "unknown_user"
    http_status = 403


class PatientDataDetectedError(DomainError):
    code = "patient_data_detected"
    http_status = 422


class InvalidQuestionError(DomainError):
    code = "invalid_question"
    http_status = 400


class UpstreamUnavailableError(DomainError):
    """LLM/embedding provider failed after allowed retries or timed out."""
    code = "upstream_unavailable"
    http_status = 503


class IndexNotReadyError(DomainError):
    code = "index_not_ready"
    http_status = 503


class IngestionError(DomainError):
    """A snapshot is invalid; the run is aborted and nothing is published."""
    code = "ingestion_error"
    http_status = 400
