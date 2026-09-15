from __future__ import annotations

from typing import Dict, Any

import numpy as np
from scipy import signal
from scipy.stats import kurtosis, skew


_HARD_CLIP_ABS = 1e50


def signal_numeric_summary(signal_arr: np.ndarray) -> Dict[str, Any]:
    x = np.asarray(signal_arr).reshape(-1)
    if x.size == 0:
        return {
            "length": 0,
            "finite_ratio": 0.0,
            "nan_count": 0,
            "posinf_count": 0,
            "neginf_count": 0,
            "abs_max": 0.0,
            "mean": 0.0,
            "std": 0.0,
        }

    finite_mask = np.isfinite(x)
    finite_x = x[finite_mask]
    return {
        "length": int(x.size),
        "finite_ratio": float(np.mean(finite_mask)),
        "nan_count": int(np.isnan(x).sum()),
        "posinf_count": int(np.isposinf(x).sum()),
        "neginf_count": int(np.isneginf(x).sum()),
        "abs_max": float(np.max(np.abs(finite_x))) if finite_x.size else 0.0,
        "mean": float(np.mean(finite_x)) if finite_x.size else 0.0,
        "std": float(np.std(finite_x)) if finite_x.size else 0.0,
    }


def sanitize_signal_array(
    signal_arr: np.ndarray,
    *,
    preserve_length: bool = True,
    clip_abs: float = _HARD_CLIP_ABS,
) -> np.ndarray:
    x = np.asarray(signal_arr, dtype=np.float64).reshape(-1)
    if x.size == 0:
        raise ValueError("信号为空")

    if preserve_length:
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    else:
        x = x[np.isfinite(x)]
        if x.size == 0:
            raise ValueError("信号中不存在有效有限值")

    if clip_abs is not None:
        x = np.clip(x, -clip_abs, clip_abs)

    return x


def detrend_signal(signal_arr: np.ndarray) -> np.ndarray:
    # 保持长度不变，避免不同分支的频率分辨率和时域长度不一致
    x = sanitize_signal_array(signal_arr, preserve_length=True)
    if x.size == 0:
        raise ValueError("信号为空")
    return signal.detrend(x, type="constant")


def compute_time_features(signal_arr: np.ndarray) -> Dict[str, float]:
    x = sanitize_signal_array(signal_arr, preserve_length=False)
    x = detrend_signal(x)
    x = sanitize_signal_array(x, preserve_length=False)

    if x.size == 0:
        return {
            "rms": 0.0,
            "peak": 0.0,
            "peak_to_peak": 0.0,
            "kurtosis": 0.0,
            "skewness": 0.0,
            "crest_factor": 0.0,
            "mean": 0.0,
            "std": 0.0,
        }

    # RMS / 峰值等使用原尺度；clip 已经做过，避免平方溢出
    mean_val = float(np.mean(x))
    std_val = float(np.std(x))
    rms_val = float(np.sqrt(np.mean(x * x)))
    peak_val = float(np.max(np.abs(x)))
    pp_val = float(np.max(x) - np.min(x))
    crest_val = float(peak_val / rms_val) if rms_val > 1e-12 else 0.0

    # 偏度 / 峭度只与线性缩放无关，归一化后计算更稳
    scale = float(np.max(np.abs(x))) if x.size else 0.0
    if scale > 1e-12:
        x_stat = x / scale
    else:
        x_stat = x.copy()

    try:
        kurt_val = float(kurtosis(x_stat, fisher=False, bias=False, nan_policy="omit"))
        if not np.isfinite(kurt_val):
            kurt_val = 0.0
    except Exception:
        kurt_val = 0.0

    try:
        skew_val = float(skew(x_stat, bias=False, nan_policy="omit"))
        if not np.isfinite(skew_val):
            skew_val = 0.0
    except Exception:
        skew_val = 0.0

    return {
        "rms": rms_val,
        "peak": peak_val,
        "peak_to_peak": pp_val,
        "kurtosis": kurt_val,
        "skewness": skew_val,
        "crest_factor": crest_val,
        "mean": mean_val,
        "std": std_val,
    }
