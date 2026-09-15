from __future__ import annotations

from typing import Dict, List

import numpy as np
from scipy import signal
from scipy.fft import rfft, rfftfreq, irfft

from app.core.feature_extractor import detrend_signal, sanitize_signal_array


def bandpass_filter(
    signal_arr: np.ndarray,
    fs_hz: float,
    lowcut_hz: float = 2.0,
    highcut_hz: float = 215.0,
    order: int = 4,
) -> np.ndarray:
    """
    零相位带通滤波（带数值清洗）
    """
    x = sanitize_signal_array(signal_arr, preserve_length=True)
    nyq = fs_hz / 2.0

    lowcut_hz = max(lowcut_hz, 0.1)
    highcut_hz = min(highcut_hz, nyq - 1e-6)

    if lowcut_hz >= highcut_hz:
        raise ValueError(
            f"滤波参数非法: lowcut_hz={lowcut_hz}, highcut_hz={highcut_hz}, nyq={nyq}"
        )

    # filtfilt 对长度有要求，太短时不再强行滤波，避免 pad/数值问题
    min_len = max(16, 3 * (order + 1))
    if x.size < min_len:
        return detrend_signal(x)

    # 这里必须使用 SOS 形式。当前样本 fs=25600Hz、带通 2~215Hz 时，
    # 归一化频率非常小，直接 b/a + filtfilt 容易数值不稳定，产生巨幅值/溢出。
    sos = signal.butter(
        order,
        [lowcut_hz / nyq, highcut_hz / nyq],
        btype="bandpass",
        output="sos",
    )
    y = signal.sosfiltfilt(sos, x)
    return sanitize_signal_array(y, preserve_length=True)


def integrate_acc_to_vel(
    signal_arr: np.ndarray,
    fs_hz: float,
    min_valid_hz: float = 2.0,
) -> np.ndarray:
    """
    频域积分：加速度 -> 速度
    - 先清洗 NaN/Inf 和极端值
    - 对 >= min_valid_hz 的频率积分，避免低频漂移和直流发散
    """
    x = detrend_signal(signal_arr)
    x = sanitize_signal_array(x, preserve_length=True)
    n = len(x)

    spec = rfft(x)
    freqs = rfftfreq(n, d=1.0 / fs_hz)

    vel_spec = np.zeros_like(spec, dtype=np.complex128)
    mask = freqs >= max(min_valid_hz, 1e-6)
    vel_spec[mask] = spec[mask] / (1j * 2.0 * np.pi * freqs[mask])

    vel = irfft(vel_spec, n=n)
    vel = np.real(vel)
    return sanitize_signal_array(vel, preserve_length=True)


def is_acc_signal(signal_type: str) -> bool:
    if not signal_type:
        return False
    text = str(signal_type).strip().lower()
    return ("加速度" in text) or ("acc" in text) or ("acceleration" in text)


def preprocess_signal_for_speed_judgement(
    signal_arr: np.ndarray,
    fs_hz: float,
    signal_type: str = "",
    lowcut_hz: float = 2.0,
    highcut_hz: float = 215.0,
) -> np.ndarray:
    """
    转速判定前的统一预处理：
    - 先统一清洗数值
    - 如果是加速度：先积分到速度
    - 然后统一做带通
    """
    x = sanitize_signal_array(signal_arr, preserve_length=True)
    x = detrend_signal(x)

    if is_acc_signal(signal_type):
        x = integrate_acc_to_vel(x, fs_hz, min_valid_hz=lowcut_hz)

    x = bandpass_filter(x, fs_hz, lowcut_hz=lowcut_hz, highcut_hz=highcut_hz, order=4)
    return sanitize_signal_array(x, preserve_length=True)


def compute_fft(signal_arr: np.ndarray, fs_hz: float, window: str = "hann") -> tuple[np.ndarray, np.ndarray]:
    """
    输入为预处理后的信号；此处再做一次轻量数值清洗，避免 rfft 被 NaN/Inf 污染。
    """
    x = detrend_signal(signal_arr)
    x = sanitize_signal_array(x, preserve_length=True)
    n = len(x)
    if n == 0:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)

    win = signal.get_window(window, n)
    xw = x * win
    amps = np.abs(rfft(xw))
    freqs = rfftfreq(n, d=1.0 / fs_hz)
    amps = sanitize_signal_array(amps, preserve_length=True)
    return freqs, amps


def extract_top_peaks(
    freqs: np.ndarray,
    amps: np.ndarray,
    fmin: float = 0.5,
    fmax: float | None = None,
    top_n: int = 20,
    min_distance_hz: float = 1.0,
    prominence_ratio: float = 0.015,
) -> List[Dict[str, float]]:
    if len(freqs) == 0 or len(amps) == 0:
        return []

    if fmax is None:
        fmax = float(freqs[-1])

    mask = (freqs >= fmin) & (freqs <= fmax)
    f = freqs[mask]
    a = amps[mask]
    if len(f) < 5:
        return []

    max_amp = float(np.max(a)) if np.max(a) > 0 else 1.0
    df = float(f[1] - f[0]) if len(f) > 1 else 1.0
    distance_bins = max(1, int(min_distance_hz / max(df, 1e-9)))
    peaks, _ = signal.find_peaks(a, prominence=max_amp * prominence_ratio, distance=distance_bins)

    items = [{"freq_hz": float(f[i]), "amp": float(a[i])} for i in peaks]
    items.sort(key=lambda item: item["amp"], reverse=True)
    return items[:top_n]


def generate_candidate_bases(
    low_peaks: List[Dict[str, float]],
    df_hz: float,
    min_hz: float = 3.0,
    max_hz: float = 100.0,
    max_divisor: int = 10,
) -> List[float]:
    candidates = set()
    for peak in low_peaks:
        freq = float(peak["freq_hz"])
        if min_hz <= freq <= max_hz:
            candidates.add(round(freq, 4))
        for divisor in range(2, max_divisor + 1):
            base = freq / divisor
            if not (min_hz <= base <= max_hz):
                continue
            quant = max(df_hz / 2.0, 1e-6)
            base_q = round(round(base / quant) * quant, 4)
            candidates.add(base_q)
    return sorted(candidates)


def analyze_harmonic_family(
    freqs: np.ndarray,
    amps: np.ndarray,
    base_freq_hz: float,
    max_order: int = 10,
    tol_ratio: float = 0.03,
    min_prominence_ratio: float = 2.5,
    min_global_ratio: float = 0.02,
) -> List[Dict[str, float]]:
    hits: List[Dict[str, float]] = []
    if base_freq_hz <= 0 or len(freqs) < 3:
        return hits

    df_hz = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 1.0
    global_max_amp = float(np.max(amps)) if len(amps) > 0 else 0.0
    if global_max_amp <= 0:
        return hits

    max_valid_order = min(max_order, int(freqs[-1] / max(base_freq_hz, 1e-9)))

    for order in range(1, max_valid_order + 1):
        target = base_freq_hz * order
        tol = max(base_freq_hz * tol_ratio, df_hz)

        mask = np.abs(freqs - target) <= tol
        if not np.any(mask):
            continue

        idxs = np.where(mask)[0]
        idx = idxs[int(np.argmax(amps[idxs]))]
        if idx <= 0 or idx >= len(amps) - 1:
            continue

        actual = float(freqs[idx])
        amp = float(amps[idx])
        is_local_peak = (amps[idx] >= amps[idx - 1]) and (amps[idx] >= amps[idx + 1])
        if not is_local_peak:
            continue

        noise_band = max(3 * tol, 3 * df_hz)
        noise_mask = (freqs >= target - noise_band) & (freqs <= target + noise_band)
        local_amps = amps[noise_mask]
        if len(local_amps) < 3:
            continue

        local_noise_floor = float(np.median(local_amps))
        if local_noise_floor <= 1e-12:
            local_noise_floor = 1e-12

        prominence_ratio = amp / local_noise_floor
        if prominence_ratio < min_prominence_ratio:
            continue
        if amp < global_max_amp * min_global_ratio:
            continue

        hits.append(
            {
                "order": order,
                "target_freq_hz": float(target),
                "actual_freq_hz": actual,
                "amp": amp,
                "rel_error_pct": float(abs(actual - target) / max(target, 1e-9) * 100.0),
                "prominence_ratio": prominence_ratio,
            }
        )

    return hits
