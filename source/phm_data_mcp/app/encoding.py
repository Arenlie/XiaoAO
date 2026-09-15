from __future__ import annotations

import base64
import gzip
import json
from typing import Any


def bytes_from_mongo_binary(value: Any) -> bytes:
    """Convert Mongo byteValues variants to raw bytes without decoding float samples."""
    if value is None:
        raise ValueError("byteValues为空")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return base64.b64decode("".join(value.split()))
    # pymongo Binary is a bytes subclass, so normal production values are handled above.
    raise ValueError(f"不支持的byteValues类型: {type(value).__name__}")


def encode_waveform(raw: bytes) -> tuple[str, dict[str, Any]]:
    if len(raw) % 4 != 0:
        raise ValueError(f"波形字节长度{len(raw)}不是float32字节数4的整数倍")
    data = base64.b64encode(raw).decode("ascii")
    decode = {
        "required": True,
        "encoding": "base64",
        "serialization": "float32_array",
        "byte_order": "little",
        "python": "np.frombuffer(base64.b64decode(data), dtype='<f4')",
    }
    return data, decode


def encode_large_json(value: Any) -> tuple[str, dict[str, Any]]:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6)
    data = base64.b64encode(compressed).decode("ascii")
    decode = {
        "required": True,
        "encoding": "base64",
        "serialization": "json",
        "compression": "gzip",
        "charset": "utf-8",
        "python": "json.loads(gzip.decompress(base64.b64decode(data)).decode('utf-8'))",
    }
    return data, decode
