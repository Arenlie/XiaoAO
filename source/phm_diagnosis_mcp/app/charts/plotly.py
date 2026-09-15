from __future__ import annotations

from typing import Any

import numpy as np


TITLES = {
    "time_waveform": ("时域波形", "时间 / s", "幅值"),
    "frequency_spectrum": ("频谱", "频率 / Hz", "幅值"),
    "envelope_spectrum": ("解调频谱 / 包络谱", "频率 / Hz", "幅值"),
    "order_spectrum": ("阶比谱", "阶次 / X", "幅值"),
    "power_spectrum": ("功率谱 / PSD", "频率 / Hz", "功率谱密度"),
    "cepstrum": ("倒频谱 / 倒谱", "倒频率 / s", "幅值"),
    "envelope_order_spectrum": ("解调阶比谱", "阶次 / X", "幅值"),
    "feature_trend": ("特征趋势", "时间", "数值"),
    "temperature_trend": ("温度趋势", "时间", "温度"),
}


def _sample_indexes(size: int, max_points: int) -> np.ndarray:
    if size <= max_points:
        return np.arange(size)
    return np.linspace(0, size - 1, max_points, dtype=int)


def xy_chart(chart_type: str, x: np.ndarray, y: np.ndarray, max_points: int = 4000) -> dict[str, Any]:
    title, x_label, y_label = TITLES[chart_type]
    idx = _sample_indexes(min(x.size, y.size), max(100, int(max_points)))
    return {
        "traces": [{"x": [float(v) for v in x[idx]], "y": [float(v) for v in y[idx]], "type": "scatter", "mode": "lines"}],
        "layout": {"title": title, "xaxis": {"title": x_label}, "yaxis": {"title": y_label}, "showlegend": False},
    }


def trend_chart(chart_type: str, series: list[dict[str, Any]], max_points: int = 4000) -> dict[str, Any]:
    title, x_label, y_label = TITLES[chart_type]
    traces: list[dict[str, Any]] = []
    for item in series:
        samples = item.get("samples") or []
        if not samples:
            continue
        idx = _sample_indexes(len(samples), max(100, int(max_points)))
        traces.append({
            "name": item.get("name") or item.get("kpi_id") or "trend",
            "x": [samples[int(i)].get("time") for i in idx],
            "y": [float(samples[int(i)].get("value") or 0.0) for i in idx],
            "type": "scatter",
            "mode": "lines",
        })
    return {
        "traces": traces,
        "layout": {"title": title, "xaxis": {"title": x_label}, "yaxis": {"title": y_label}, "showlegend": True},
    }
