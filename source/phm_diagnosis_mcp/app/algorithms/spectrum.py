from __future__ import annotations

import numpy as np


def _windowed(values: np.ndarray) -> tuple[np.ndarray, float]:
    y = np.asarray(values, dtype=float)
    if y.size < 2:
        return y, 1.0
    y = y - float(np.mean(y))
    window = np.hanning(y.size)
    gain = float(np.sum(window) / y.size) or 1.0
    return y * window, gain


def frequency_spectrum(values: np.ndarray, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    y, gain = _windowed(values)
    if y.size < 2:
        return np.array([]), np.array([])
    freq = np.fft.rfftfreq(y.size, d=1.0 / sample_rate_hz)
    amp = np.abs(np.fft.rfft(y)) * 2.0 / max(y.size * gain, 1e-12)
    return freq, amp


def power_spectrum(values: np.ndarray, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(values, dtype=float)
    if y.size < 2:
        return np.array([]), np.array([])
    y = y - float(np.mean(y))
    window = np.hanning(y.size)
    scaled = y * window
    scale = sample_rate_hz * float(np.sum(window**2))
    freq = np.fft.rfftfreq(y.size, d=1.0 / sample_rate_hz)
    psd = np.abs(np.fft.rfft(scaled)) ** 2 / max(scale, 1e-12)
    if psd.size > 2:
        psd[1:-1] *= 2.0
    return freq, psd


def analytic_envelope(values: np.ndarray) -> np.ndarray:
    y = np.asarray(values, dtype=float)
    n = y.size
    if n == 0:
        return y
    spec = np.fft.fft(y)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1 : n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1 : (n + 1) // 2] = 2.0
    return np.abs(np.fft.ifft(spec * h))


def envelope_spectrum(values: np.ndarray, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    envelope = analytic_envelope(values)
    return frequency_spectrum(envelope, sample_rate_hz)


def cepstrum(values: np.ndarray, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(values, dtype=float)
    if y.size < 2:
        return np.array([]), np.array([])
    y = y - float(np.mean(y))
    log_spectrum = np.log(np.maximum(np.abs(np.fft.fft(y)), 1e-12))
    ceps = np.abs(np.fft.ifft(log_spectrum).real)
    quefrency = np.arange(y.size, dtype=float) / sample_rate_hz
    half = y.size // 2
    return quefrency[1:half], ceps[1:half]


def top_peaks(x: np.ndarray, y: np.ndarray, limit: int = 10) -> list[dict[str, float]]:
    if x.size < 3 or y.size < 3:
        return []
    indexes = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]))[0] + 1
    if indexes.size == 0:
        return []
    ranked = indexes[np.argsort(y[indexes])[::-1]][:limit]
    return [{"x": float(x[i]), "amplitude": float(y[i])} for i in ranked]


def order_spectrum(freq: np.ndarray, amp: np.ndarray, speed_rpm: float) -> tuple[np.ndarray, np.ndarray]:
    rotating_hz = speed_rpm / 60.0
    if rotating_hz <= 0:
        raise ValueError("speed_rpm必须大于0")
    return freq / rotating_hz, amp

