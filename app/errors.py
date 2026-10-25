"""Structured application errors. Every failure surfaces as a typed error, never a bare traceback."""
from __future__ import annotations


class AppError(Exception):
    http_status = 500
    kind = "internal_error"

    def __init__(self, message: str, stage: str = "", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"type": self.kind, "message": self.message, "stage": self.stage, "details": self.details}


class RequestError(AppError):
    http_status = 422
    kind = "invalid_request"


class NoDataError(AppError):
    http_status = 422
    kind = "no_data"


class LLMError(AppError):
    http_status = 502
    kind = "model_error"


class DataError(AppError):
    http_status = 502
    kind = "data_error"
