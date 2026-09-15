from __future__ import annotations

import base64
import binascii
from typing import Tuple

import numpy as np

from app.schemas import DataQuality, SignalPayload


def decode_signal(payload: SignalPayload) -> np.ndarray:
    """Decode MCP waveform payload to a 1-D float64 NumPy array."""
    if payload.samples is not None:
        try:
            return np.asarray(payload.samples, dtype=np.float64).reshape(-1)
        except Exception as exc:
            raise ValueError(f"samples cannot be converted to float64: {exc}") from exc

    assert payload.float32_base64 is not None
    try:
        raw = base64.b64decode(payload.float32_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid float32_base64: {exc}") from exc

    if len(raw) == 0 or len(raw) % 4 != 0:
        raise ValueError("float32_base64 byte length must be a non-zero multiple of 4")
    return np.frombuffer(raw, dtype="<f4").astype(np.float64, copy=True)


def encode_float32_base64(signal_arr: np.ndarray) -> str:
    x = np.asarray(signal_arr, dtype="<f4").reshape(-1)
    return base64.b64encode(x.tobytes(order="C")).decode("ascii")


def validate_and_sanitize_signal(
    signal_arr: np.ndarray,
    *,
    fs_hz: float,
    min_samples: int,
    max_samples: int,
    min_finite_ratio: float,
) -> Tuple[np.ndarray, DataQuality]:
    raw = np.asarray(signal_arr, dtype=np.float64).reshape(-1)
    n = int(raw.size)
    if n < min_samples:
        raise ValueError(f"waveform too short: {n} samples; minimum is {min_samples}")
    if n > max_samples:
        raise ValueError(f"waveform too long: {n} samples; maximum is {max_samples}")
    if fs_hz <= 0:
        raise ValueError("fs_hz must be > 0")

    finite_mask = np.isfinite(raw)
    finite_ratio = float(np.mean(finite_mask)) if n else 0.0
    if finite_ratio < min_finite_ratio:
        raise ValueError(
            f"finite sample ratio is too low: {finite_ratio:.4f}; minimum is {min_finite_ratio:.4f}"
        )

    finite_values = raw[finite_mask]
    min_value = float(np.min(finite_values)) if finite_values.size else None
    max_value = float(np.max(finite_values)) if finite_values.size else None

    x = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)
    if np.allclose(x, 0.0):
        raise ValueError("waveform is all zero after sanitization")

    quality = DataQuality(
        n_samples=n,
        finite_ratio=round(finite_ratio, 6),
        duration_s=float(n / fs_hz),
        df_hz=float(fs_hz / n),
        min_value=min_value,
        max_value=max_value,
    )
    return x, quality
