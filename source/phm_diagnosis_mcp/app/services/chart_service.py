from __future__ import annotations

from typing import Any

import numpy as np

from app.algorithms.preprocessing import preprocess
from app.algorithms.spectrum import (
    cepstrum,
    envelope_spectrum,
    frequency_spectrum,
    order_spectrum,
    power_spectrum,
    top_peaks,
)
from app.algorithms.time_domain import calculate_time_domain
from app.algorithms.trend import analyze_trend_series
from app.charts.plotly import trend_chart, xy_chart
from app.decoding import decode_trend, decode_waveform
from app.mechanism.rules import chart_findings


WAVEFORM_CHARTS = {
    "time_waveform",
    "frequency_spectrum",
    "envelope_spectrum",
    "order_spectrum",
    "power_spectrum",
    "cepstrum",
    "envelope_order_spectrum",
}
TREND_CHARTS = {"feature_trend", "temperature_trend"}


class ChartService:
    def analyze(self, chart_type: str, waveform: dict[str, Any] | None, trend: dict[str, Any] | None, speed_rpm: float | None, options: dict[str, Any] | None) -> dict[str, Any]:
        if chart_type not in WAVEFORM_CHARTS | TREND_CHARTS:
            raise ValueError(f"不支持的chart_type: {chart_type}")
        options = options or {}
        max_points = int(options.get("max_points") or 4000)

        if chart_type in TREND_CHARTS:
            if trend is None:
                raise ValueError(f"{chart_type}需要trend")
            series, meta = decode_trend(trend)
            metrics = {"series": analyze_trend_series(series)}
            return {
                "success": True,
                "chart_type": chart_type,
                "point_id": meta.get("point_id"),
                "metrics": metrics,
                "findings": chart_findings(chart_type, metrics, speed_rpm),
                "chart": trend_chart(chart_type, series, max_points),
            }

        if waveform is None:
            raise ValueError(f"{chart_type}需要waveform")
        values, meta = decode_waveform(waveform)
        fs = float(meta["sample_rate_hz"])
        preprocess_method = str(options.get("preprocess") or "demean")
        median_window = int(options.get("median_window") or 5)
        y = preprocess(values, preprocess_method, median_window)

        if chart_type == "time_waveform":
            x = np.arange(values.size, dtype=float) / fs
            metrics = calculate_time_domain(values)
            chart = xy_chart(chart_type, x, values, max_points)
        elif chart_type == "frequency_spectrum":
            x, amp = frequency_spectrum(y, fs)
            metrics = self._frequency_metrics(x, amp, speed_rpm)
            chart = xy_chart(chart_type, x, amp, max_points)
        elif chart_type == "power_spectrum":
            x, amp = power_spectrum(y, fs)
            metrics = self._frequency_metrics(x, amp, speed_rpm)
            chart = xy_chart(chart_type, x, amp, max_points)
        elif chart_type == "envelope_spectrum":
            x, amp = envelope_spectrum(y, fs)
            metrics = self._frequency_metrics(x, amp, speed_rpm)
            chart = xy_chart(chart_type, x, amp, max_points)
        elif chart_type == "cepstrum":
            x, amp = cepstrum(y, fs)
            peaks = top_peaks(x, amp)
            metrics = {"top_peaks": [{"quefrency_s": p["x"], "amplitude": p["amplitude"]} for p in peaks]}
            chart = xy_chart(chart_type, x, amp, max_points)
        elif chart_type in {"order_spectrum", "envelope_order_spectrum"}:
            if not speed_rpm or speed_rpm <= 0:
                raise ValueError(f"{chart_type}需要speed_rpm")
            freq, amp = envelope_spectrum(y, fs) if chart_type == "envelope_order_spectrum" else frequency_spectrum(y, fs)
            x, amp = order_spectrum(freq, amp, speed_rpm)
            peaks = top_peaks(x, amp)
            integer_hits = []
            for p in peaks:
                nearest = round(p["x"])
                if nearest >= 1 and abs(p["x"] - nearest) <= 0.05:
                    integer_hits.append({"order": int(nearest), "amplitude": p["amplitude"]})
            metrics = {"top_peaks": [{"order": p["x"], "amplitude": p["amplitude"]} for p in peaks], "integer_order_hits": integer_hits}
            chart = xy_chart(chart_type, x, amp, max_points)
        else:
            raise ValueError(f"不支持的chart_type: {chart_type}")

        return {
            "success": True,
            "chart_type": chart_type,
            "point_no": meta.get("point_no"),
            "metrics": metrics,
            "findings": chart_findings(chart_type, metrics, speed_rpm),
            "chart": chart,
        }

    @staticmethod
    def _frequency_metrics(freq: np.ndarray, amp: np.ndarray, speed_rpm: float | None) -> dict[str, Any]:
        peaks = top_peaks(freq, amp)
        result: dict[str, Any] = {
            "top_peaks": [{"frequency_hz": p["x"], "amplitude": p["amplitude"]} for p in peaks]
        }
        if speed_rpm and speed_rpm > 0:
            rotating_hz = speed_rpm / 60.0
            hits = []
            for peak in peaks:
                order_value = peak["x"] / rotating_hz
                nearest = round(order_value)
                if nearest >= 1 and abs(order_value - nearest) <= 0.05:
                    hits.append({
                        "order": int(nearest),
                        "frequency_hz": peak["x"],
                        "amplitude": peak["amplitude"],
                    })
            result["integer_order_hits"] = hits
        return result
