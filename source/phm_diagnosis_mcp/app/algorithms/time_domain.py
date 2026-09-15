from __future__ import annotations

import numpy as np


def calculate_time_domain(values: np.ndarray) -> dict[str, float | int]:
    y = np.asarray(values, dtype=float)
    if y.size == 0:
        return {"count": 0}

    mean = float(np.mean(y))
    centered = y - mean
    std = float(np.std(y))
    rms = float(np.sqrt(np.mean(y**2)))
    abs_peak = float(np.max(np.abs(y)))
    mean_abs = float(np.mean(np.abs(y)))
    root_abs_mean = float(np.mean(np.sqrt(np.abs(y))))
    safe_std = max(std, 1e-12)

    return {
        "count": int(y.size),
        "mean": mean,
        "std": std,
        "rms": rms,
        "min": float(np.min(y)),
        "max": float(np.max(y)),
        "abs_peak": abs_peak,
        "peak_to_peak": float(np.ptp(y)),
        "skewness": float(np.mean(centered**3) / safe_std**3),
        "kurtosis": float(np.mean(centered**4) / safe_std**4),
        "crest_factor": float(abs_peak / max(rms, 1e-12)),
        "impulse_factor": float(abs_peak / max(mean_abs, 1e-12)),
        "shape_factor": float(rms / max(mean_abs, 1e-12)),
        "clearance_factor": float(abs_peak / max(root_abs_mean**2, 1e-12)),
    }
