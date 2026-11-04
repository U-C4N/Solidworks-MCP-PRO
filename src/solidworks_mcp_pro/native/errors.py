"""Stable, structured failures at the native CAD boundary."""
from __future__ import annotations

import math


def _json_detail(value):
    """Keep native error evidence serializable, including SDK out-parameter tuples."""
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _json_detail(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_detail(item) for item in value]
    return {"unserializable_type": type(value).__name__}


class CadError(RuntimeError):
    def __init__(self, code: str, message: str | None = None, **details):
        if message is None:
            message, code = code, "NATIVE_ERROR"
        self.code, self.message, self.details = code, message, details
        super().__init__(f"{code}: {message}")

    def result(self) -> dict:
        return {**_json_detail(self.details), "ok": False, "code": self.code, "error": self.message}

