from __future__ import annotations

from typing import Any

import numpy as np


def analyze_trend_series(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in series:
        samples = item.get("samples") or []
        values = np.asarray([float(x["value"]) for x in samples if x.get("value") is not None], dtype=float)
        if values.size == 0:
            continue

        segment = max(1, values.size // 10)
        start_level = float(np.median(values[:segment]))
        end_level = float(np.median(values[-segment:]))
        scale = max(abs(start_level), float(np.std(values)), 1e-12)
        change_ratio = float((end_level - start_level) / scale)
        change_percent = float((end_level - start_level) / max(abs(start_level), 1e-12) * 100.0)

        if values.size > 1:
            index = np.arange(values.size, dtype=float)
            corr = float(np.corrcoef(index, values)[0, 1]) if float(np.std(values)) > 1e-12 else 0.0
            steps = np.diff(values)
            step_std = float(np.std(steps))
            max_step_z = float(np.max(np.abs(steps - np.mean(steps))) / max(step_std, 1e-12)) if steps.size else 0.0
        else:
            corr = 0.0
            max_step_z = 0.0

        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        robust_z = np.abs(values - median) / max(1.4826 * mad, 1e-12)
        outlier_ratio = float(np.mean(robust_z > 3.0))

        if abs(change_ratio) < 0.05 and max_step_z < 3.0:
            label = "平稳"
        elif max_step_z >= 5.0:
            label = "突变"
        elif change_ratio > 0:
            label = "上升"
        else:
            label = "下降"

        results.append({
            "kpi_id": str(item.get("kpi_id") or ""),
            "name": item.get("name"),
            "unit": item.get("unit"),
            "count": int(values.size),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "mean": float(np.mean(values)),
            "start_level": start_level,
            "end_level": end_level,
            "change_ratio": change_ratio,
            "change_percent": change_percent,
            "corr_with_time": corr,
            "max_step_z": max_step_z,
            "outlier_ratio": outlier_ratio,
            "trend_label": label,
        })
    return results
