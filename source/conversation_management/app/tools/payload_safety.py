from __future__ import annotations

from typing import Any

_BASE64_KEYS = {"values_base64", "payload_base64"}


def _estimated_decoded_bytes(text: str) -> int:
    # Base64 expands bytes by roughly 4/3. This estimate intentionally avoids decoding.
    padding = 2 if text.endswith("==") else 1 if text.endswith("=") else 0
    return max(0, (len(text) * 3) // 4 - padding)


def sanitize_large_payloads(value: Any) -> Any:
    """Remove large binary bodies from persisted/model-visible structures.

    Data MCP Base64 remains available only in the current run's transient store. The
    sanitizer preserves decode metadata and surrounding query metadata while replacing
    the actual Base64 string with a compact audit marker.
    """

    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if key in _BASE64_KEYS and isinstance(item, str):
                clean[key] = {
                    "omitted": True,
                    "reason": "binary_payload_not_persisted",
                    "base64_chars": len(item),
                    "estimated_bytes": _estimated_decoded_bytes(item),
                }
            else:
                clean[key] = sanitize_large_payloads(item)
        return clean
    if isinstance(value, list):
        return [sanitize_large_payloads(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_large_payloads(item) for item in value]
    return value


def contains_base64_payload(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _BASE64_KEYS and isinstance(item, str) and item:
                return True
            if contains_base64_payload(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(contains_base64_payload(item) for item in value)
    return False
