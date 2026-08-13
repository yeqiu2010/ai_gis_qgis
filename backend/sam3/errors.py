"""Typed SAM3 integration failures."""

from __future__ import annotations


class Sam3Error(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "sam3_error",
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable

    def as_payload(self) -> dict[str, object]:
        return {
            "success": False,
            "error": str(self),
            "error_code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }
