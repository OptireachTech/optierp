"""Uniform application error hierarchy.

Every 4xx response uses the envelope ``{"detail": "...", "code": "ERR_CODE", "field": "..."}``
(Section 9, rule 8). FastAPI exception handlers are registered in ``app.main``.
"""

from typing import Any


class AppError(Exception):
    """Base class for all expected application errors."""

    status_code: int = 400
    code: str = "ERR_BAD_REQUEST"

    def __init__(self, detail: str, *, code: str | None = None, field: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.field = field
        if code is not None:
            self.code = code

    def envelope(self) -> dict[str, Any]:
        return {"detail": self.detail, "code": self.code, "field": self.field}


class NotFoundError(AppError):
    status_code = 404
    code = "ERR_NOT_FOUND"


class ValidationError(AppError):
    """Business-rule validation failure (mirrors frappe.ValidationError)."""

    status_code = 422
    code = "ERR_VALIDATION"


class DuplicateError(AppError):
    status_code = 409
    code = "ERR_DUPLICATE"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "ERR_PERMISSION_DENIED"


class AuthenticationError(AppError):
    status_code = 401
    code = "ERR_AUTHENTICATION"


class RateLimitedError(AppError):
    status_code = 429
    code = "ERR_RATE_LIMITED"


class DocstatusError(AppError):
    """Raised on illegal docstatus transitions (e.g. editing a submitted doc)."""

    status_code = 409
    code = "ERR_DOCSTATUS"


class WorkflowError(AppError):
    status_code = 409
    code = "ERR_WORKFLOW"
