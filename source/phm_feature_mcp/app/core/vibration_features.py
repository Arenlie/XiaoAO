from __future__ import annotations

from typing import Dict, Iterable

import numpy as np
from scipy import signal
from scipy.integrate import trapezoid
from scipy.stats import kurtosis, skew

from app.schemas import EnvelopeBand, FeatureSet, FrequencyBand

_EPS = 1e-12

TIME_BASIC_KEYS = (
    "mean",
    "rms",
    "std",
    "peak",
    "peak_to_peak",
    "kurtosis",
    "crest_factor",
)
TIME_STANDARD_KEYS = (
    "mean",
    "absolute_mean",
    "rms",
    "variance",
    "std",
    "peak",
    "peak_to_peak",
    "skewness",
    "kurtosis",
    "crest_factor",
    "shape_factor",
    "impulse_factor",
    "clearance_factor",
    "energy",
    "zero_crossing_rate",
)
FREQUENCY_KEYS = (
    "dominant_frequency_hz",
    "spectral_centroid_hz",
    "rms_frequency_hz",
    "spectral_std_hz",
    "spectral_entropy",
    "total_psd_power",
)
ENVELOPE_KEYS = (
    "rms",
    "peak",
    "peak_to_peak",
    "kurtosis",
    "crest_factor",
    "dominant_frequency_hz",
    "spectral_entropy",
)

FEATURE_SET_CATALOG: Dict[str, list[str]] = {
    "basic": [f"time_domain.{key}" for key in TIME_BASIC_KEYS],
    "standard": [
        *[f"time_domain.{key}" for key in TIME_STANDARD_KEYS],
        *[f"frequency_domain.{key}" for key in FREQUENCY_KEYS],
    ],
    "bearing": [
        *[f"time_domain.{key}" for key in TIME_STANDARD_KEYS],
        *[f"frequency_domain.{key}" for key in FREQUENCY_KEYS],
        *[f"envelope_domain.{key}" for key in ENVELOPE_KEYS],
    ],
    "full": [
        *[f"time_domain.{key}" for key in TIME_STANDARD_KEYS],
        *[f"frequency_domain.{key}" for key in FREQUENCY_KEYS],
        *[f"envelope_domain.{key}" for key in ENVELOPE_KEYS],
        "band_energy.<custom_band>.energy",
        "band_energy.<custom_band>.ratio",
    ],
}


def _finite(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0


def _select(source: Dict[str, float], keys: Iterable[str]) -> Dict[str, float]:
    return {key: source[key] for key in keys if key in source}


def compute_time_domain_features(x: np.ndarray, *, detrend: bool = True) -> Dict[str, float]:
    y = np.asarray(x, dtype=np.float64).reshape(-1)
    if detrend:
        y = signal.detrend(y, type="constant")

    mean = float(np.mean(y))
    abs_mean = float(np.mean(np.abs(y)))
    rms = float(np.sqrt(np.mean(y * y)))
    variance = float(np.var(y))
    std = float(np.std(y))
    peak = float(np.max(np.abs(y)))
    peak_to_peak = float(np.ptp(y))
    root_abs_mean = float(np.mean(np.sqrt(np.abs(y))))

    crest_factor = peak / rms if rms > _EPS else 0.0
    shape_factor = rms / abs_mean if abs_mean > _EPS else 0.0
    impulse_factor = peak / abs_mean if abs_mean > _EPS else 0.0
    clearance_factor = peak / (root_abs_mean * root_abs_mean) if root_abs_mean > _EPS else 0.0

    scale = peak if peak > _EPS else 1.0
    stat_y = y / scale
    kurt = _finite(float(kurtosis(stat_y, fisher=False, bias=False, nan_policy="omit")))
    skw = _finite(float(skew(stat_y, bias=False, nan_policy="omit")))

    if len(y) > 1:
        signs = np.signbit(y)
        zero_crossing_rate = float(np.mean(signs[1:] != signs[:-1]))
    else:
        zero_crossing_rate = 0.0

    return {
        "mean": mean,
        "absolute_mean": abs_mean,
        "rms": rms,
        "variance": variance,
        "std": std,
        "peak": peak,
        "peak_to_peak": peak_to_peak,
        "skewness": skw,
        "kurtosis": kurt,
        "crest_factor": crest_factor,
        "shape_factor": shape_factor,
        "impulse_factor": impulse_factor,
        "clearance_factor": clearance_factor,
        "energy": float(np.sum(y * y)),
        "zero_crossing_rate": zero_crossing_rate,
    }


def compute_psd(x: np.ndarray, fs_hz: float, *, window: str = "hann") -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(x, dtype=np.float64).reshape(-1)
    nperseg = min(len(y), 16384)
    if nperseg < 8:
        raise ValueError("waveform is too short for PSD calculation")
    freqs, psd = signal.welch(
        y,
        fs=fs_hz,
        window=window,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        detrend="constant",
        scaling="density",
    )
    return freqs.astype(np.float64), np.maximum(psd.astype(np.float64), 0.0)


def compute_frequency_domain_features(
    x: np.ndarray,
    fs_hz: float,
    *,
    window: str = "hann",
) -> tuple[Dict[str, float], np.ndarray, np.ndarray]:
    freqs, psd = compute_psd(x, fs_hz, window=window)
    power_sum = float(np.sum(psd))
    if power_sum <= _EPS:
        return {
            "dominant_frequency_hz": 0.0,
            "spectral_centroid_hz": 0.0,
            "rms_frequency_hz": 0.0,
            "spectral_std_hz": 0.0,
            "spectral_entropy": 0.0,
            "total_psd_power": 0.0,
        }, freqs, psd

    dominant_idx = int(np.argmax(psd[1:]) + 1) if len(psd) > 1 else 0
    centroid = float(np.sum(freqs * psd) / power_sum)
    rms_frequency = float(np.sqrt(np.sum((freqs * freqs) * psd) / power_sum))
    spectral_std = float(np.sqrt(np.sum(((freqs - centroid) ** 2) * psd) / power_sum))

    probabilities = psd / power_sum
    nz = probabilities > 0
    entropy = -float(np.sum(probabilities[nz] * np.log(probabilities[nz])))
    if len(probabilities) > 1:
        entropy /= float(np.log(len(probabilities)))

    total_power = float(trapezoid(psd, freqs)) if len(freqs) > 1 else power_sum
    return {
        "dominant_frequency_hz": float(freqs[dominant_idx]),
        "spectral_centroid_hz": centroid,
        "rms_frequency_hz": rms_frequency,
        "spectral_std_hz": spectral_std,
        "spectral_entropy": _finite(entropy),
        "total_psd_power": total_power,
    }, freqs, psd


def compute_band_energy(
    freqs: np.ndarray,
    psd: np.ndarray,
    bands: Iterable[FrequencyBand],
) -> Dict[str, float]:
    result: Dict[str, float] = {}
    total = float(trapezoid(psd, freqs)) if len(freqs) > 1 else float(np.sum(psd))
    for band in bands:
        mask = (freqs >= band.low_hz) & (freqs < band.high_hz)
        if int(np.count_nonzero(mask)) >= 2:
            energy = float(trapezoid(psd[mask], freqs[mask]))
        elif np.any(mask):
            energy = float(np.sum(psd[mask]))
        else:
            energy = 0.0
        result[f"{band.name}.energy"] = energy
        result[f"{band.name}.ratio"] = energy / total if total > _EPS else 0.0
    return result


def _bandpass_for_envelope(x: np.ndarray, fs_hz: float, band: EnvelopeBand | None) -> np.ndarray:
    y = np.asarray(x, dtype=np.float64).reshape(-1)
    y = signal.detrend(y, type="constant")
    if band is None:
        return y

    nyquist = fs_hz / 2.0
    if band.high_hz >= nyquist:
        raise ValueError(
            f"envelope_band.high_hz={band.high_hz} must be below Nyquist frequency {nyquist:.3f}Hz"
        )
    sos = signal.butter(
        band.filter_order,
        [band.low_hz, band.high_hz],
        btype="bandpass",
        fs=fs_hz,
        output="sos",
    )
    return signal.sosfiltfilt(sos, y)


def compute_envelope_domain_features(
    x: np.ndarray,
    fs_hz: float,
    *,
    window: str = "hann",
    envelope_band: EnvelopeBand | None = None,
) -> Dict[str, float]:
    """Extract scalar envelope features. FFT/Hilbert remain internal implementation details."""
    y = _bandpass_for_envelope(x, fs_hz, envelope_band)
    envelope = np.abs(signal.hilbert(y))
    envelope = signal.detrend(envelope, type="constant")

    td = compute_time_domain_features(envelope, detrend=False)
    fd, _, _ = compute_frequency_domain_features(envelope, fs_hz, window=window)
    return {
        "rms": td["rms"],
        "peak": td["peak"],
        "peak_to_peak": td["peak_to_peak"],
        "kurtosis": td["kurtosis"],
        "crest_factor": td["crest_factor"],
        "dominant_frequency_hz": fd["dominant_frequency_hz"],
        "spectral_entropy": fd["spectral_entropy"],
    }


def extract_feature_groups(
    x: np.ndarray,
    fs_hz: float,
    *,
    feature_set: FeatureSet,
    detrend: bool,
    window: str,
    bands: Iterable[FrequencyBand],
    envelope_band: EnvelopeBand | None,
) -> tuple[Dict[str, float], Dict[str, float], Dict[str, float], Dict[str, float]]:
    """Single internal pipeline that returns only scalar feature groups."""
    all_time = compute_time_domain_features(x, detrend=detrend)
    if feature_set == "basic":
        return _select(all_time, TIME_BASIC_KEYS), {}, {}, {}

    all_freq, freqs, psd = compute_frequency_domain_features(x, fs_hz, window=window)
    time_domain = _select(all_time, TIME_STANDARD_KEYS)
    frequency_domain = _select(all_freq, FREQUENCY_KEYS)
    envelope_domain: Dict[str, float] = {}

    if feature_set in {"bearing", "full"}:
        envelope_domain = _select(
            compute_envelope_domain_features(
                x,
                fs_hz,
                window=window,
                envelope_band=envelope_band,
            ),
            ENVELOPE_KEYS,
        )

    band_energy = compute_band_energy(freqs, psd, bands) if bands else {}
    return time_domain, frequency_domain, envelope_domain, band_energy
