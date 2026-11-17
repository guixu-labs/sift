"""Encoding for JSON values embedded in inline JavaScript."""

from __future__ import annotations

import json
from typing import Any


def dumps_for_script(value: Any, *, indent: int | None = None) -> str:
    """Serialize JSON so data cannot terminate its containing HTML script element."""
    payload = json.dumps(value, ensure_ascii=False, indent=indent)
    # U+2028/U+2029 were parsed as JavaScript string terminators by older
    # engines even though they are valid JSON characters.
    return (
        payload.replace("<", r"\u003c")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )
