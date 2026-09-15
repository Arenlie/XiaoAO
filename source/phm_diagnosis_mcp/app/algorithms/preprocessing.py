from __future__ import annotations

import numpy as np


def median_filter(values: np.ndarray, window: int = 5) -> np.ndarray:
    window = int(window or 5)
    if window <= 1 or values.size == 0:
        return values.astype(float, copy=True)
    if window % 2 == 0:
        window += 1
    if window > values.size:
        window = values.size if values.size % 2 else max(1, values.size - 1)
    if window <= 1:
        return values.astype(float, copy=True)
    half = window // 2
    padded = np.pad(values, (half, half), mode="edge")
    return np.asarray([np.median(padded[i : i + window]) for i in range(values.size)], dtype=float)


def preprocess(values: np.ndarray, method: str = "demean", median_window: int = 5) -> np.ndarray:
    y = np.nan_to_num(np.asarray(values, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    if method == "none":
        return y
    if method == "demean":
        return y - float(np.mean(y)) if y.size else y
    if method == "detrend":
        if y.size < 3:
            return y
        x = np.arange(y.size, dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        return y - (slope * x + intercept)
    if method == "median_filter":
        return median_filter(y, median_window)
    raise ValueError("preprocess只支持none、demean、detrend、median_filter")
