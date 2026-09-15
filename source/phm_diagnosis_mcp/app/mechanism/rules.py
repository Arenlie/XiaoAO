from __future__ import annotations

from typing import Any

from app.algorithms.config import TIME_DOMAIN, TREND


def chart_findings(chart_type: str, metrics: dict[str, Any], speed_rpm: float | None = None) -> list[str]:
    findings: list[str] = []
    if chart_type == "time_waveform":
        kurtosis = float(metrics.get("kurtosis") or 0.0)
        crest = float(metrics.get("crest_factor") or 0.0)
        if kurtosis >= TIME_DOMAIN["kurtosis_high"] or crest >= TIME_DOMAIN["crest_factor_high"]:
            findings.append("时域冲击性明显，建议重点检查轴承、松动或碰磨类故障。")
        elif kurtosis >= TIME_DOMAIN["kurtosis_medium"] or crest >= TIME_DOMAIN["crest_factor_medium"]:
            findings.append("时域存在一定冲击性，需要结合包络谱进一步确认。")
    elif chart_type in {"frequency_spectrum", "envelope_spectrum", "power_spectrum"}:
        peaks = metrics.get("top_peaks") or []
        if peaks:
            first = peaks[0]
            findings.append(f"主要峰值位于 {float(first.get('frequency_hz') or 0.0):.4g} Hz。")
        if speed_rpm and speed_rpm > 0:
            hits = metrics.get("integer_order_hits") or []
            if hits:
                orders = "、".join(f"{int(x['order'])}X" for x in hits[:4])
                findings.append(f"检测到转频相关整数倍频成分：{orders}。")
    elif chart_type in {"order_spectrum", "envelope_order_spectrum"}:
        hits = metrics.get("integer_order_hits") or []
        if hits:
            orders = "、".join(f"{int(x['order'])}X" for x in hits[:5])
            findings.append(f"阶比谱存在明显整数阶成分：{orders}。")
    elif chart_type in {"feature_trend", "temperature_trend"}:
        stats = metrics.get("series") or []
        changed = [x for x in stats if abs(float(x.get("change_ratio") or 0.0)) >= TREND["change_ratio_medium"]]
        abrupt = [x for x in stats if float(x.get("max_step_z") or 0.0) >= TREND["jump_z_medium"]]
        if changed:
            findings.append("部分趋势存在明显方向性变化。")
        if abrupt:
            findings.append("部分趋势存在突变，需要结合工况和采集质量复核。")
    return findings


def assess_abnormality(
    waveform_analysis: dict[str, Any] | None,
    feature_trend_analysis: list[dict[str, Any]],
    temperature_trend_analysis: list[dict[str, Any]],
) -> list[str]:
    """先判断数据是否存在异常，再允许进入故障机理识别。频谱形态本身不能证明设备有故障。"""
    evidence: list[str] = []

    if waveform_analysis:
        time_metrics = waveform_analysis.get("time_domain") or {}
        kurtosis = float(time_metrics.get("kurtosis") or 0.0)
        crest = float(time_metrics.get("crest_factor") or 0.0)
        if kurtosis >= TIME_DOMAIN["kurtosis_medium"]:
            evidence.append(f"时域峭度偏高（{kurtosis:.3g}）。")
        if crest >= TIME_DOMAIN["crest_factor_medium"]:
            evidence.append(f"波峰因子偏高（{crest:.3g}）。")

    for group_name, analyses in (
        ("特征趋势", feature_trend_analysis),
        ("温度趋势", temperature_trend_analysis),
    ):
        for item in analyses:
            for series in item.get("series") or []:
                name = str(series.get("name") or series.get("kpi_id") or "未命名指标")
                change_ratio = abs(float(series.get("change_ratio") or 0.0))
                max_step_z = float(series.get("max_step_z") or 0.0)
                if change_ratio >= TREND["change_ratio_medium"]:
                    evidence.append(f"{group_name} {name} 存在明显变化。")
                if max_step_z >= TREND["jump_z_medium"]:
                    evidence.append(f"{group_name} {name} 存在明显突变。")

    return list(dict.fromkeys(evidence))


def build_fault_hypotheses(
    waveform_analysis: dict[str, Any] | None,
    abnormal_evidence: list[str],
) -> list[dict[str, Any]]:
    """只有先存在客观异常证据时，才根据频谱/时域形态生成故障假设。"""
    if not waveform_analysis or not abnormal_evidence:
        return []

    point_no = waveform_analysis.get("point_no") or "未标识测点"
    time_metrics = waveform_analysis.get("time_domain") or {}
    freq = waveform_analysis.get("frequency_spectrum") or {}
    envelope = waveform_analysis.get("envelope_spectrum") or {}

    harmonics = {int(x["order"]): float(x["amplitude"]) for x in freq.get("integer_order_hits") or []}
    amp1 = harmonics.get(1, 0.0)
    amp2 = harmonics.get(2, 0.0)
    amp3 = harmonics.get(3, 0.0)
    amp4 = harmonics.get(4, 0.0)
    kurtosis = float(time_metrics.get("kurtosis") or 0.0)
    crest = float(time_metrics.get("crest_factor") or 0.0)
    envelope_peaks = envelope.get("top_peaks") or []

    candidates: list[dict[str, Any]] = []
    if amp1 > 0 and amp1 >= max(amp2, 1e-12) * 1.5:
        candidates.append({
            "fault_type": "rotor_unbalance",
            "fault_name": "转子不平衡",
            "confidence": 0.78,
            "evidence": [f"{point_no} 的1X转频成分占主导。"],
        })
    if amp1 > 0 and amp2 >= amp1 * 0.45:
        candidates.append({
            "fault_type": "misalignment",
            "fault_name": "不对中",
            "confidence": 0.72,
            "evidence": [f"{point_no} 同时存在较明显1X和2X成分。"],
        })
    if sum(1 for value in (amp1, amp2, amp3, amp4) if value > 0) >= 3:
        candidates.append({
            "fault_type": "mechanical_looseness",
            "fault_name": "机械松动",
            "confidence": 0.68,
            "evidence": [f"{point_no} 存在连续多阶转频谐波。"],
        })
    if kurtosis >= TIME_DOMAIN["kurtosis_medium"] and (crest >= TIME_DOMAIN["crest_factor_medium"] or envelope_peaks):
        candidates.append({
            "fault_type": "bearing_impact",
            "fault_name": "轴承冲击类异常",
            "confidence": 0.70,
            "evidence": [f"{point_no} 时域冲击指标升高，并存在包络频域证据。"],
        })

    return sorted(candidates, key=lambda item: float(item["confidence"]), reverse=True)
