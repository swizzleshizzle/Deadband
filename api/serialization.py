"""JSON that keeps NUMERIC exact (spec D4): every Decimal serializes as a
string, never a float. Applied app-wide via default_response_class so a new
endpoint cannot opt out by accident."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse


def _default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


class DeadbandJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content, default=_default, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
