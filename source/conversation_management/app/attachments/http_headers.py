from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def content_disposition(filename: str, *, inline: bool) -> str:
    """Return a UTF-8 filename-safe Content-Disposition header value.

    Starlette encodes response header values as latin-1. Non-ASCII filenames must
    therefore be carried in RFC 5987 ``filename*=UTF-8''...`` while ``filename=``
    stays ASCII-compatible.
    """

    disposition = "inline" if inline else "attachment"
    suffix = Path(filename).suffix.lower()
    fallback = f"attachment{suffix}" if suffix and suffix.isascii() else "attachment"
    encoded = quote(filename, safe="", encoding="utf-8", errors="strict")
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
