from __future__ import annotations

"""
仅保留 mechanic_rule_detect_speed 及其直接依赖。

本版满足三点要求：
1. 输入以加速度波形为主；若未显式提供 vel_data，则默认由加速度内部积分得到速度；
2. 仅当进入“需要加速度复核”的分支后，若加速度转频与速度转频不一致，
   则把双谱数据信息交给大模型仲裁器做二选一仲裁；
3. 算法层只接受一个可选的 llm_conflict_judge 回调，不直接负责模型连接与外部 API 调用。

说明：
- 不包含 mechanic_rule_validate / controller / 候选排序 / 最终融合等其他逻辑；
- 不再依赖 settings；
- LLM 接入由 infrastructure 层实现，算法层保持对外部服务无感知。
"""

from dataclasses import dataclass, asdict
from math import ceil, floor, isclose
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple
from typing import Dict, Any

import numpy as np
from scipy import fftpack


# =========================
# 数据结构
# =========================

@dataclass
class MechanicSpeedResult:
    freq_hz: Optional[float]
    rpm: Optional[float]
    confidence: float
    supported: bool
    reason: str
    evidence: List[str]
    conflicts: List[str]
    top_candidates: List[Dict[str, Any]]
    debug: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =========================
# 基础工具函数
# =========================
def _safe_array(data: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if data is None:
        return None
    arr = np.asarray(data, dtype=float).reshape(-1)
    if len(arr) == 0:
        return None
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    if np.allclose(arr, 0.0):
        return None
    return arr


def _normalize_signal(data: np.ndarray) -> np.ndarray:
    data = np.asarray(data, dtype=float)
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    if len(data) == 0:
        return data
    data = data - np.mean(data)
    max_abs = float(np.max(np.abs(data)))
    if max_abs > 0:
        data = data / max_abs
    return data


def _compact_speed_result_for_llm(
        result: MechanicSpeedResult,
        source_name: str,
) -> Dict[str, Any]:
    top_candidates = []
    for item in list(result.top_candidates or [])[:3]:
        try:
            top_candidates.append({
                "freq_hz": round(float(item.get("freq_hz", 0.0)), 6),
                "rpm": round(float(item.get("rpm", 0.0)), 3),
                "score": round(float(item.get("score", 0.0)), 4),
                "source": str(item.get("source", source_name)),
            })
        except Exception:
            continue

    return {
        "source": source_name,
        "freq_hz": None if result.freq_hz is None else round(float(result.freq_hz), 6),
        "rpm": None if result.rpm is None else round(float(result.rpm), 3),
        "confidence": round(float(result.confidence or 0.0), 4),
        "supported": bool(result.supported),
        "reason": str(result.reason or "")[:300],
        "evidence": result.evidence,
        "conflicts": result.conflicts,
        "top_candidates": top_candidates,
    }


def fft_spectrum(signal: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray]:
    """计算单边频谱。"""
    signal = np.asarray(signal, dtype=float)
    n = len(signal)
    if n < 2:
        raise ValueError("信号长度不足，无法进行频谱分析")
    f = fs * np.arange(n // 2) / n
    fft_data = np.abs(np.fft.fft(signal))[: n // 2] * 2.0 / n
    return f, fft_data


def acc2dis(data: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray]:
    """加速度积分为速度、位移。保持与原始算法一致的简单频域去直流方式。"""
    data = np.asarray(data, dtype=float)
    a_dt = data / fs
    v = np.cumsum(a_dt)
    v_fft = np.fft.fft(v)
    v_fft[:2] = 0
    v_fft[-3:] = 0
    v_filtered = np.real(np.fft.ifft(v_fft))

    v_dt = v_filtered / fs
    d = np.cumsum(v_dt)
    d_fft = np.fft.fft(d)
    d_fft[:2] = 0
    d_fft[-3:] = 0
    d_filtered = np.real(np.fft.ifft(d_fft))

    return v_filtered * 1000.0, d_filtered * 1e6


def fft_filter(signal: np.ndarray, lpf1: float, lpf2: float, fs: float) -> np.ndarray:
    """FFT 带通滤波。保持原始实现风格。"""
    signal = np.asarray(signal, dtype=float)
    yy = fftpack.fft(signal)
    m = len(yy)
    if m < 2:
        raise ValueError("信号长度不足，无法滤波")
    k = m / fs
    yy[: floor(k * lpf1) + 1] = 0
    yy[ceil(k * lpf2 - 1):] = 0
    return 2.0 * np.real(fftpack.ifft(yy))


def _is_harmonic(target: float, base: float, tol: float, max_order: int = 10) -> bool:
    if base == 0:
        return False
    for i in range(1, max_order + 1):
        if isclose(target, i * base, abs_tol=tol):
            return True
    return False


def remove_close_peaks(
        frequencies: Sequence[float],
        amplitudes: Sequence[float],
        min_distance_hz: float = 5.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """去除相近频率，只保留幅值更大的峰。"""
    frequencies = np.asarray(frequencies, dtype=float)
    amplitudes = np.asarray(amplitudes, dtype=float)
    if len(frequencies) != len(amplitudes):
        raise ValueError("frequencies 与 amplitudes 长度不一致")
    if len(frequencies) == 0:
        return frequencies, amplitudes

    sorted_idx = np.argsort(amplitudes)[::-1]
    keep = np.ones(len(frequencies), dtype=bool)

    for i in range(len(sorted_idx)):
        idx_i = sorted_idx[i]
        if not keep[idx_i]:
            continue
        for j in range(i + 1, len(sorted_idx)):
            idx_j = sorted_idx[j]
            if abs(frequencies[idx_i] - frequencies[idx_j]) < min_distance_hz:
                keep[idx_j] = False

    return frequencies[keep], amplitudes[keep]


def _build_harmonic_result_for_seed(
        seed_freq: float,
        top_freqs: np.ndarray,
        tol: float,
        min_candidate_hz: Optional[float] = None,
        max_candidate_hz: Optional[float] = None,
) -> Dict[str, Any]:
    harmonic_counts = {k: 0 for k in ["half", "exact", "double", "triple", "quadruple"]}
    freq_candidates: Dict[str, float] = {}

    mode_to_candidate = {
        "half": 2.0 * seed_freq,
        "exact": seed_freq,
        "double": seed_freq / 2.0,
        "triple": seed_freq / 3.0,
        "quadruple": seed_freq / 4.0,
    }

    def _candidate_allowed(freq_hz: float) -> bool:
        if freq_hz <= 0:
            return False
        if min_candidate_hz is not None and freq_hz < min_candidate_hz:
            return False
        if max_candidate_hz is not None and freq_hz > max_candidate_hz:
            return False
        return True

    for mode, cand_freq in mode_to_candidate.items():
        if not _candidate_allowed(cand_freq):
            continue

        for freq in top_freqs:
            if _is_harmonic(freq, cand_freq, tol):
                harmonic_counts[mode] += 1
                freq_candidates[mode] = cand_freq

    valid_modes = [m for m in harmonic_counts if m in freq_candidates]
    if not valid_modes:
        return {
            "seed_freq": float(seed_freq),
            "harmonic_counts": harmonic_counts,
            "freq_candidates": freq_candidates,
            "max_count": 0,
            "best_mode": None,
            "best_freq_candidate": None,
        }

    max_count = max(harmonic_counts[m] for m in valid_modes)
    candidates = [m for m in valid_modes if harmonic_counts[m] == max_count]
    priority_order = ["exact", "half", "double", "triple", "quadruple"]
    best_mode = next((m for m in priority_order if m in candidates), None)
    best_freq_candidate = freq_candidates.get(best_mode) if best_mode else None

    return {
        "seed_freq": float(seed_freq),
        "harmonic_counts": harmonic_counts,
        "freq_candidates": freq_candidates,
        "max_count": int(max_count),
        "best_mode": best_mode,
        "best_freq_candidate": best_freq_candidate,
    }


def _select_priority_seeds(top_freqs: np.ndarray, top_amps: np.ndarray) -> List[float]:
    if len(top_freqs) == 0:
        return []

    seeds: List[float] = []
    min_freq = float(np.min(top_freqs))
    seeds.append(min_freq)

    top_two_indices = np.argsort(top_amps)[-2:][::-1]
    for idx in top_two_indices:
        freq = float(top_freqs[idx])
        if freq not in seeds:
            seeds.append(freq)
    return seeds


def _score_candidate(
        candidate_hz: float,
        support_count: int,
        top_freqs: np.ndarray,
        top_amps: np.ndarray,
        single_strong_peak: bool = False,
) -> float:
    if candidate_hz <= 0:
        return 0.0

    score = 0.15
    if single_strong_peak:
        score += 0.25

    if support_count >= 4:
        score += 0.45
    elif support_count == 3:
        score += 0.32
    elif support_count == 2:
        score += 0.18
    elif support_count == 1:
        score += 0.08

    close_direct = np.any(np.abs(top_freqs - candidate_hz) <= max(1.0, candidate_hz * 0.03))
    if close_direct:
        score += 0.10

    score = min(0.98, max(0.0, score))
    return round(float(score), 4)


def _has_multiple_harmonic_relation(freq_a: float, freq_b: float, tol_hz: float, max_multiple: int = 4) -> Tuple[
    bool, Optional[int]]:
    if freq_a <= 0 or freq_b <= 0:
        return False, None
    hi = max(freq_a, freq_b)
    lo = min(freq_a, freq_b)
    if lo <= 0:
        return False, None
    for k in range(2, max_multiple + 1):
        if abs(hi - k * lo) <= tol_hz:
            return True, k
    return False, None


def _candidate_is_close_smaller_competitor(
        main_item: Dict[str, Any],
        other_item: Dict[str, Any],
        tol_hz: float,
        score_margin: float = 0.10,
) -> bool:
    main_hz = float(main_item.get("freq_hz", 0.0) or 0.0)
    other_hz = float(other_item.get("freq_hz", 0.0) or 0.0)
    if main_hz <= 0 or other_hz <= 0:
        return False
    if other_hz >= main_hz:
        return False

    main_support = int(main_item.get("support_count", 0) or 0)
    other_support = int(other_item.get("support_count", 0) or 0)
    main_score = float(main_item.get("score", 0.0) or 0.0)
    other_score = float(other_item.get("score", 0.0) or 0.0)

    support_close = other_support >= max(1, main_support - 1)
    score_close = other_score >= max(0.0, main_score - score_margin)

    abs_close = (main_hz - other_hz) <= max(3.0 * tol_hz, 12.0)
    rel_close = (other_hz / max(main_hz, 1e-8)) >= 0.55

    return support_close and score_close and (abs_close or rel_close)


def _should_trigger_acc_review(
        vel_result: MechanicSpeedResult,
        tol_hz: float,
        low_freq_hz: float = 15.0,
        high_freq_hz: float = 35.0,
) -> Dict[str, Any]:
    reasons: List[str] = []
    main_hz = float(vel_result.freq_hz or 0.0)
    top_candidates = list(vel_result.top_candidates or [])

    if main_hz <= 0:
        reasons.append("速度谱未形成有效主候选，触发加速度谱复核")
        return {
            "triggered": True,
            "reasons": reasons,
            "competitors": [],
        }

    if main_hz < low_freq_hz or main_hz > high_freq_hz:
        reasons.append(
            f"速度谱主候选 {main_hz:.4f}Hz 超出稳定优先区间 [{low_freq_hz:.1f}, {high_freq_hz:.1f}]Hz，触发加速度谱复核"
        )

    competitors: List[Dict[str, Any]] = []
    main_item = None
    for item in top_candidates:
        hz = float(item.get("freq_hz", 0.0) or 0.0)
        if hz > 0 and abs(hz - main_hz) <= max(tol_hz, 0.5):
            main_item = item
            break
    if main_item is None:
        main_item = {
            "freq_hz": main_hz,
            "score": float(vel_result.confidence or 0.0),
            "support_count": 0,
        }

    for item in top_candidates:
        hz = float(item.get("freq_hz", 0.0) or 0.0)
        if hz <= 0 or abs(hz - main_hz) <= max(tol_hz, 0.5):
            continue

        harmonic_related, multiple = _has_multiple_harmonic_relation(main_hz, hz, tol_hz=max(tol_hz, 0.5),
                                                                     max_multiple=4)
        close_smaller = _candidate_is_close_smaller_competitor(main_item, item, tol_hz=max(tol_hz, 0.5),
                                                               score_margin=0.10)

        if close_smaller or harmonic_related:
            competitors.append({
                "freq_hz": round(hz, 6),
                "score": round(float(item.get("score", 0.0) or 0.0), 6),
                "support_count": int(item.get("support_count", 0) or 0),
                "best_mode": item.get("best_mode"),
                "close_smaller": close_smaller,
                "harmonic_related": harmonic_related,
                "harmonic_multiple": multiple,
            })

    close_smaller_competitors = [c for c in competitors if c["close_smaller"]]
    harmonic_competitors = [c for c in competitors if c["harmonic_related"]]

    if close_smaller_competitors:
        comp_desc = ", ".join(f"{c['freq_hz']:.4f}Hz(score={c['score']:.3f},support={c['support_count']})" for c in
                              close_smaller_competitors[:3])
        reasons.append(f"速度谱存在证据链接近且频率偏小的竞争候选：{comp_desc}")

    if harmonic_competitors:
        comp_desc = ", ".join(
            f"{c['freq_hz']:.4f}Hz(~{c['harmonic_multiple']}x关系)" if c[
                'harmonic_multiple'] else f"{c['freq_hz']:.4f}Hz"
            for c in harmonic_competitors[:3]
        )
        reasons.append(f"速度谱主候选与竞争候选存在倍数谐波关系：{comp_desc}")

    return {
        "triggered": len(reasons) > 0,
        "reasons": reasons,
        "competitors": competitors,
    }


# =========================
# 单谱识别
# =========================

def _mechanic_rule_detect_speed_single(
        data: np.ndarray,
        fs: float,
        *,
        spectrum_name: str = "velocity",
        num_fre: int = 10,
        min_amp: float = 0.0,
        tol_hz: float = 2.0,
        bandpass_low_hz: float = 5.0,
        bandpass_high_hz: float = 215.0,
        strong_peak_ratio: float = 5.0,
        remove_close_distance_hz: float = 5.0,
        min_candidate_hz: Optional[float] = None,
        max_candidate_hz: Optional[float] = None,
) -> MechanicSpeedResult:
    data = np.asarray(data, dtype=float)
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)

    if len(data) < 8:
        return MechanicSpeedResult(
            freq_hz=None,
            rpm=None,
            confidence=0.0,
            supported=False,
            reason=f"{spectrum_name} 机理模型无法识别：信号长度不足",
            evidence=[],
            conflicts=["信号长度不足"],
            top_candidates=[],
            debug={"spectrum_name": spectrum_name},
        )

    data_filtered = fft_filter(data, bandpass_low_hz, bandpass_high_hz, fs)

    f, fft_data = fft_spectrum(data_filtered, fs)
    resolution_hz = float(fs / len(data)) if len(data) > 0 else 0.0

    top_indices = np.argsort(fft_data)[-int(num_fre):][::-1]
    top_freqs = f[top_indices]
    top_amps = fft_data[top_indices]

    mask = top_amps >= float(min_amp)
    top_freqs = top_freqs[mask]
    top_amps = top_amps[mask]
    top_freqs, top_amps = remove_close_peaks(top_freqs, top_amps, min_distance_hz=remove_close_distance_hz)

    if len(top_amps) < 2:
        return MechanicSpeedResult(
            freq_hz=None,
            rpm=None,
            confidence=0.0,
            supported=False,
            reason=f"{spectrum_name} 机理模型无法识别：有效峰数量不足",
            evidence=[],
            conflicts=["有效峰数量不足"],
            top_candidates=[],
            debug={
                "spectrum_name": spectrum_name,
                "top_freqs": top_freqs.tolist(),
                "top_amps": top_amps.tolist(),
                "resolution_hz": resolution_hz,
                "fft_freq": f.tolist(),
                "fft_amp": fft_data.tolist(),
            },
        )

    evidence: List[str] = []
    conflicts: List[str] = []
    candidate_details: List[Dict[str, Any]] = []

    if top_amps[1] > 0 and float(top_amps[0] / top_amps[1]) > float(strong_peak_ratio):
        freq_hz = float(top_freqs[0])
        confidence = _score_candidate(freq_hz, support_count=1, top_freqs=top_freqs, top_amps=top_amps,
                                      single_strong_peak=True)
        evidence.append(f"{spectrum_name} 谱低频主峰显著强于次峰，主峰/次峰={top_amps[0] / top_amps[1]:.2f}")
        evidence.append(f"{spectrum_name} 谱按单强峰规则直接给出候选")
        return MechanicSpeedResult(
            freq_hz=freq_hz,
            rpm=freq_hz * 60.0,
            confidence=confidence,
            supported=confidence >= 0.55,
            reason=f"{spectrum_name} 机理模型采用单强峰直判，候选转频={freq_hz:.4f}Hz",
            evidence=evidence,
            conflicts=conflicts,
            top_candidates=[
                {
                    "freq_hz": freq_hz,
                    "rpm": freq_hz * 60.0,
                    "score": confidence,
                    "mode": "single_strong_peak",
                    "support_count": 1,
                    "source": spectrum_name,
                }
            ],
            debug={
                "spectrum_name": spectrum_name,
                "top_freqs": top_freqs.tolist(),
                "top_amps": top_amps.tolist(),
                "resolution_hz": resolution_hz,
                "single_strong_peak_ratio": float(top_amps[0] / top_amps[1]),
                "fft_freq": f.tolist(),
                "fft_amp": fft_data.tolist(),
            },
        )

    seeds = _select_priority_seeds(top_freqs, top_amps)
    seed_results = [
        _build_harmonic_result_for_seed(
            seed,
            top_freqs,
            tol_hz,
            min_candidate_hz=min_candidate_hz,
            max_candidate_hz=max_candidate_hz,
        )
        for seed in seeds
    ]

    if not seed_results:
        return MechanicSpeedResult(
            freq_hz=None,
            rpm=None,
            confidence=0.0,
            supported=False,
            reason=f"{spectrum_name} 机理模型无法识别：未形成可用候选",
            evidence=[],
            conflicts=["未形成可用 seed 频率"],
            top_candidates=[],
            debug={
                "spectrum_name": spectrum_name,
                "top_freqs": top_freqs.tolist(),
                "top_amps": top_amps.tolist(),
                "resolution_hz": resolution_hz,
            },
        )

    max_support = max(res["max_count"] for res in seed_results)
    max_results = [res for res in seed_results if res["max_count"] == max_support]

    best_result: Optional[Dict[str, Any]] = None
    for seed in seeds:
        matched = next((res for res in max_results if float(res["seed_freq"]) == float(seed)), None)
        if matched is not None:
            best_result = matched
            break

    if best_result is None or best_result.get("best_freq_candidate") is None:
        return MechanicSpeedResult(
            freq_hz=None,
            rpm=None,
            confidence=0.0,
            supported=False,
            reason=f"{spectrum_name} 机理模型无法识别：倍频支持不足",
            evidence=[],
            conflicts=["倍频支持不足，无法确定 best_freq_candidate"],
            top_candidates=[],
            debug={
                "spectrum_name": spectrum_name,
                "seed_results": seed_results,
                "top_freqs": top_freqs.tolist(),
                "top_amps": top_amps.tolist(),
                "resolution_hz": resolution_hz,
            },
        )

    for res in seed_results:
        freq_hz = res.get("best_freq_candidate")
        if freq_hz is None or freq_hz <= 0:
            continue
        if min_candidate_hz is not None and freq_hz < min_candidate_hz:
            continue
        if max_candidate_hz is not None and freq_hz > max_candidate_hz:
            continue
        support_count = int(res["max_count"])
        score = _score_candidate(float(freq_hz), support_count, top_freqs, top_amps)
        candidate_details.append(
            {
                "freq_hz": round(float(freq_hz), 6),
                "rpm": round(float(freq_hz) * 60.0, 6),
                "score": score,
                "support_count": support_count,
                "seed_freq": round(float(res["seed_freq"]), 6),
                "best_mode": res.get("best_mode"),
                "harmonic_counts": res.get("harmonic_counts", {}),
                "source": spectrum_name,
            }
        )

    candidate_details.sort(
        key=lambda x: (
            x["support_count"],
            1 if x.get("best_mode") == "exact" else 0,
            x["score"],
        ),
        reverse=True,
    )

    final_freq_hz = float(best_result["best_freq_candidate"])
    final_support = int(best_result["max_count"])
    final_confidence = _score_candidate(final_freq_hz, final_support, top_freqs, top_amps)

    evidence.append(
        f"{spectrum_name} 谱在候选集合 {seeds} 中，选择了支持数最多的模式：{best_result.get('best_mode')}"
    )
    evidence.append(
        f"{spectrum_name} 谱候选转频 {final_freq_hz:.4f}Hz 的谐波支持数为 {final_support}"
    )
    if best_result.get("best_mode") == "exact":
        evidence.append(f"{spectrum_name} 谱更符合‘峰本身即基频’的解释")
    else:
        evidence.append(f"{spectrum_name} 谱候选来自倍频反推，说明高阶倍频可能强于 1X")

    if final_support < 2:
        conflicts.append(f"{spectrum_name} 谱谐波支持较弱，证据有限")

    reason = (
        f"{spectrum_name} 机理模型识别转频={final_freq_hz:.4f}Hz，"
        f"best_mode={best_result.get('best_mode')}，support_count={final_support}"
    )
    return MechanicSpeedResult(
        freq_hz=final_freq_hz,
        rpm=final_freq_hz * 60.0,
        confidence=final_confidence,
        supported=final_confidence >= 0.55,
        reason=reason,
        evidence=evidence,
        conflicts=conflicts,
        top_candidates=candidate_details[:5],
        debug={
            "spectrum_name": spectrum_name,
            "top_freqs": top_freqs.tolist(),
            "top_amps": top_amps.tolist(),
            "resolution_hz": resolution_hz,
            "seed_results": seed_results,
            "selected_seed": best_result.get("seed_freq"),
            "selected_mode": best_result.get("best_mode"),
            "fft_freq": f.tolist(),
            "fft_amp": fft_data.tolist(),
        },
    )


# =========================
# 绘图
# =========================

def _plot_dual_spectra_with_buttons(
        vel_result: Optional[MechanicSpeedResult],
        acc_result: Optional[MechanicSpeedResult],
        freq_min_hz: float = 5.0,
        freq_max_hz: float = 215.0,
        title: str = "速度频谱和加速度频谱",
        save_path: Optional[str] = None,
        show: bool = True,
) -> Optional[str]:
    try:
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Button
        plt.rcParams['font.sans-serif'] = ['SimHei']
        plt.rcParams['axes.unicode_minus'] = False
    except Exception:
        return None

    vel_freq = np.asarray((vel_result.debug or {}).get("fft_freq", []), dtype=float) if vel_result else np.array([])
    vel_amp = np.asarray((vel_result.debug or {}).get("fft_amp", []), dtype=float) if vel_result else np.array([])
    acc_freq = np.asarray((acc_result.debug or {}).get("fft_freq", []), dtype=float) if acc_result else np.array([])
    acc_amp = np.asarray((acc_result.debug or {}).get("fft_amp", []), dtype=float) if acc_result else np.array([])

    if len(vel_freq) == 0 and len(acc_freq) == 0:
        return None

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    plt.subplots_adjust(bottom=0.18, hspace=0.35)

    if len(vel_freq):
        mask = (vel_freq >= freq_min_hz) & (vel_freq <= freq_max_hz)
        axes[0].plot(vel_freq[mask], vel_amp[mask], linewidth=0.9)
        axes[0].set_title("速度频谱")
        axes[0].set_ylabel("Amplitude")
        axes[0].grid(True, alpha=0.3)
        if vel_result and vel_result.freq_hz is not None:
            axes[0].axvline(float(vel_result.freq_hz), linestyle="--", linewidth=1.0)
            axes[0].text(
                float(vel_result.freq_hz),
                float(np.max(vel_amp[mask])) * 0.9 if np.any(mask) else 0.0,
                f"{float(vel_result.freq_hz):.2f}Hz",
            )
    else:
        axes[0].text(0.5, 0.5, "无速度频谱数据", transform=axes[0].transAxes, ha="center", va="center")

    if len(acc_freq):
        mask = (acc_freq >= freq_min_hz) & (acc_freq <= freq_max_hz)
        axes[1].plot(acc_freq[mask], acc_amp[mask], linewidth=0.9)
        axes[1].set_title("加速度频谱")
        axes[1].set_xlabel("Frequency (Hz)")
        axes[1].set_ylabel("Amplitude")
        axes[1].grid(True, alpha=0.3)
        if acc_result and acc_result.freq_hz is not None:
            axes[1].axvline(float(acc_result.freq_hz), linestyle="--", linewidth=1.0)
            axes[1].text(
                float(acc_result.freq_hz),
                float(np.max(acc_amp[mask])) * 0.9 if np.any(mask) else 0.0,
                f"{float(acc_result.freq_hz):.2f}Hz",
            )
    else:
        axes[1].text(0.5, 0.5, "无加速度频谱数据", transform=axes[1].transAxes, ha="center", va="center")

    axes[0].set_xlim(freq_min_hz, freq_max_hz)
    axes[1].set_xlim(freq_min_hz, freq_max_hz)
    fig.suptitle(title)

    ax_save = plt.axes([0.74, 0.04, 0.10, 0.055])
    ax_close = plt.axes([0.86, 0.04, 0.10, 0.055])
    btn_save = Button(ax_save, "保存图片")
    btn_close = Button(ax_close, "关闭")

    actual_save_path: Optional[str] = None

    def _on_save(_event: Any) -> None:
        nonlocal actual_save_path
        if save_path:
            p = Path(save_path)
        else:
            p = Path.cwd() / "dual_spectrum.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(p), dpi=200, bbox_inches="tight")
        actual_save_path = str(p)
        fig.canvas.draw_idle()

    def _on_close(_event: Any) -> None:
        plt.close(fig)

    btn_save.on_clicked(_on_save)
    btn_close.on_clicked(_on_close)

    if save_path:
        _on_save(None)

    if show:
        plt.show()
    else:
        plt.close(fig)

    return actual_save_path


# =========================
# 大模型冲突仲裁
# =========================

def _llm_resolve_conflict(
        vel_result: MechanicSpeedResult,
        acc_result: MechanicSpeedResult,
        vel_result_llm: Dict[str, Any],
        acc_result_llm: Dict[str, Any],
        gate: Dict[str, Any],
        llm_conflict_judge: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
        tol_hz: float,
) -> Optional[MechanicSpeedResult]:
    if llm_conflict_judge is None:
        return None
    if vel_result.freq_hz is None or acc_result.freq_hz is None:
        return None
    if abs(float(vel_result.freq_hz) - float(acc_result.freq_hz)) <= tol_hz:
        return None

    payload = {
        "task": "当进入需要加速度复核后，速度谱与加速度谱主结果不一致，请在二者之间二选一",
        "constraints": [
            "只能在 velocity 和 acceleration 两个候选中二选一，不能输出第三个频率",
            "优先考虑 1X 直接命中、倍频链完整性、置信度、门控触发原因和冲突信息",
        ],
        "gate": gate,
        "velocity_result": vel_result_llm,
        "acceleration_result": acc_result_llm,
    }

    try:
        llm_answer = llm_conflict_judge(payload)
    except Exception as e:
        import traceback
        pass
        return MechanicSpeedResult(
            freq_hz=vel_result.freq_hz,
            rpm=vel_result.rpm,
            confidence=vel_result.confidence,
            supported=vel_result.supported,
            reason=f"LLM 仲裁调用失败，回退到速度谱结果：{e}",
            evidence=list(gate.get("reasons", [])) + ["速度谱与加速度谱结果不一致，但 LLM 仲裁调用失败"],
            conflicts=[f"速度谱={vel_result.freq_hz:.4f}Hz", f"加速度谱={acc_result.freq_hz:.4f}Hz", str(e)],
            top_candidates=list(vel_result.top_candidates or []),
            debug={
                "fusion_mode": "velocity_fallback_when_llm_error",
                "acc_review_gate": gate,
                "velocity_result": vel_result.to_dict(),
                "acceleration_result": acc_result.to_dict(),
                "llm_error": str(e),
            },
        )

    winner_source = str(llm_answer.get("winner_source", "")).strip().lower()
    if winner_source in {"velocity", "vel", "速度", "速度谱"}:
        chosen = vel_result
        chosen_name = "速度谱"
    elif winner_source in {"acceleration", "acc", "加速度", "加速度谱"}:
        chosen = acc_result
        chosen_name = "加速度谱"
    else:
        winner_freq = llm_answer.get("winner_freq_hz")
        try:
            winner_freq = float(winner_freq)
        except Exception:
            winner_freq = None
        if winner_freq is not None and abs(winner_freq - float(vel_result.freq_hz)) <= tol_hz:
            chosen = vel_result
            chosen_name = "速度谱"
        elif winner_freq is not None and abs(winner_freq - float(acc_result.freq_hz)) <= tol_hz:
            chosen = acc_result
            chosen_name = "加速度谱"
        else:
            chosen = vel_result
            chosen_name = "速度谱"

    confidence = llm_answer.get("confidence")
    try:
        confidence = float(confidence)
    except Exception:
        confidence = float(chosen.confidence)

    reason = str(llm_answer.get("reason", "")).strip() or f"LLM 仲裁选择 {chosen_name} 结果"
    llm_evidence = llm_answer.get("evidence", [])
    if not isinstance(llm_evidence, list):
        llm_evidence = [str(llm_evidence)]

    return MechanicSpeedResult(
        freq_hz=chosen.freq_hz,
        rpm=chosen.rpm,
        confidence=confidence,
        supported=confidence >= 0.55,
        reason=f"进入加速度复核且双谱不一致，{reason}",
        evidence=list(gate.get("reasons", [])) + [
            f"速度谱主结果={float(vel_result.freq_hz):.4f}Hz, conf={vel_result.confidence:.3f}",
            f"加速度谱主结果={float(acc_result.freq_hz):.4f}Hz, conf={acc_result.confidence:.3f}",
        ] + [str(x) for x in llm_evidence],
        conflicts=[f"双谱不一致：速度谱={float(vel_result.freq_hz):.4f}Hz, 加速度谱={float(acc_result.freq_hz):.4f}Hz"],
        top_candidates=[
            {
                "freq_hz": round(float(vel_result.freq_hz), 6),
                "rpm": round(float(vel_result.rpm or 0.0), 6),
                "score": round(float(vel_result.confidence), 6),
                "source": "velocity",
            },
            {
                "freq_hz": round(float(acc_result.freq_hz), 6),
                "rpm": round(float(acc_result.rpm or 0.0), 6),
                "score": round(float(acc_result.confidence), 6),
                "source": "acceleration",
            },
        ],
        debug={
            "fusion_mode": "llm_conflict_resolution",
            "acc_review_gate": gate,
            "velocity_result": vel_result.to_dict(),
            "acceleration_result": acc_result.to_dict(),
            "llm_answer": llm_answer,
            "chosen_source": chosen_name,
        },
    )


def _evaluate_candidate_on_spectrum(
        candidate_hz: float,
        top_freqs: np.ndarray,
        top_amps: np.ndarray,
        tol_hz: float,
        max_order: int = 4,
) -> Dict[str, Any]:
    """在单个谱上评估某个候选是否像真实转频。"""
    if candidate_hz <= 0 or len(top_freqs) == 0 or len(top_amps) == 0:
        return {
            "support_count": 0,
            "support_orders": [],
            "direct_present": False,
            "direct_norm_amp": 0.0,
            "harmonic_norm_sum": 0.0,
            "dominant_ratio_2x_over_1x": 0.0,
            "score": 0.0,
        }

    top_freqs = np.asarray(top_freqs, dtype=float)
    top_amps = np.asarray(top_amps, dtype=float)
    max_amp = float(np.max(top_amps)) if len(top_amps) else 0.0
    if max_amp <= 0:
        max_amp = 1.0

    support_orders: List[int] = []
    norm_amp_by_order: Dict[int, float] = {}

    for order in range(1, max_order + 1):
        target = order * candidate_hz
        idx = np.where(np.abs(top_freqs - target) <= tol_hz)[0]
        if len(idx) == 0:
            continue
        best_idx = int(idx[np.argmax(top_amps[idx])])
        support_orders.append(order)
        norm_amp_by_order[order] = float(top_amps[best_idx] / max_amp)

    direct_present = 1 in support_orders
    direct_norm_amp = norm_amp_by_order.get(1, 0.0)
    harmonic_norm_sum = sum(norm_amp_by_order.values())
    amp_2x = norm_amp_by_order.get(2, 0.0)
    dominant_ratio_2x_over_1x = amp_2x / max(direct_norm_amp, 1e-8) if amp_2x > 0 else 0.0

    score = 0.0
    score += 0.18 * len(support_orders)
    if direct_present:
        score += 0.26 + 0.18 * direct_norm_amp
    else:
        score -= 0.05
    if 2 in support_orders and 3 in support_orders:
        score += 0.06
    elif 2 in support_orders:
        score += 0.03

    # 轻度抑制“把 2X 误当 1X 的一半”这种情况，但不做激进否决
    if (not direct_present) and amp_2x >= 0.45:
        score -= 0.05
    if direct_present and dominant_ratio_2x_over_1x > 2.5:
        score -= 0.03

    score = float(max(0.0, min(0.99, score)))
    return {
        "support_count": len(support_orders),
        "support_orders": support_orders,
        "direct_present": direct_present,
        "direct_norm_amp": round(direct_norm_amp, 6),
        "harmonic_norm_sum": round(harmonic_norm_sum, 6),
        "dominant_ratio_2x_over_1x": round(dominant_ratio_2x_over_1x, 6),
        "score": round(score, 6),
    }


# =========================
# 双谱复核（仅主结果二选一）
# =========================

def _fuse_dual_spectrum_results(
        vel_result: Optional[MechanicSpeedResult],
        acc_result: Optional[MechanicSpeedResult],
        tol_hz: float,
        vel_weight: float,
        acc_weight: float,
        gate: Optional[Dict[str, Any]] = None,
        llm_conflict_judge: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> MechanicSpeedResult:
    evidence: List[str] = []
    conflicts: List[str] = []

    vel_hz = float(vel_result.freq_hz) if (
            vel_result and vel_result.freq_hz is not None and vel_result.freq_hz > 0) else None
    acc_hz = float(acc_result.freq_hz) if (
            acc_result and acc_result.freq_hz is not None and acc_result.freq_hz > 0) else None

    if vel_hz is None and acc_hz is None:
        return MechanicSpeedResult(
            freq_hz=None,
            rpm=None,
            confidence=0.0,
            supported=False,
            reason="速度谱与加速度谱均未形成可用主候选",
            evidence=[],
            conflicts=["双谱主候选为空"],
            top_candidates=[],
            debug={
                "velocity_result": vel_result.to_dict() if vel_result else None,
                "acceleration_result": acc_result.to_dict() if acc_result else None,
            },
        )

    if vel_hz is None and acc_hz is not None:
        return MechanicSpeedResult(
            freq_hz=acc_hz,
            rpm=acc_hz * 60.0,
            confidence=round(float(acc_result.confidence if acc_result else 0.0), 4),
            supported=bool(acc_result.supported if acc_result else False),
            reason=f"仅加速度谱形成主候选，采用 {acc_hz:.4f}Hz",
            evidence=["速度谱未形成可用主结果", f"加速度谱主结果={acc_hz:.4f}Hz"],
            conflicts=[],
            top_candidates=list(acc_result.top_candidates[:1]) if acc_result else [],
            debug={
                "fusion_mode": "acceleration_only_main_result",
                "velocity_result": vel_result.to_dict() if vel_result else None,
                "acceleration_result": acc_result.to_dict() if acc_result else None,
                "main_candidates": [acc_hz],
            },
        )

    if vel_hz is not None and acc_hz is None:
        return MechanicSpeedResult(
            freq_hz=vel_hz,
            rpm=vel_hz * 60.0,
            confidence=round(float(vel_result.confidence if vel_result else 0.0), 4),
            supported=bool(vel_result.supported if vel_result else False),
            reason=f"仅速度谱形成主候选，采用 {vel_hz:.4f}Hz",
            evidence=[f"速度谱主结果={vel_hz:.4f}Hz", "加速度谱未形成可用主结果"],
            conflicts=[],
            top_candidates=list(vel_result.top_candidates[:1]) if vel_result else [],
            debug={
                "fusion_mode": "velocity_only_main_result",
                "velocity_result": vel_result.to_dict() if vel_result else None,
                "acceleration_result": acc_result.to_dict() if acc_result else None,
                "main_candidates": [vel_hz],
            },
        )

    assert vel_hz is not None and acc_hz is not None

    # 满足新增要求：当进入加速度复核且双谱主结果不一致时，把信息交给大模型仲裁
    if abs(vel_hz - acc_hz) > tol_hz and gate is not None:
        vel_result_llm = _compact_speed_result_for_llm(vel_result, "velocity")
        acc_result_llm = _compact_speed_result_for_llm(acc_result, "acceleration")
        llm_result = _llm_resolve_conflict(
            vel_result=vel_result,
            acc_result=acc_result,
            vel_result_llm=vel_result_llm,
            acc_result_llm=acc_result_llm,
            gate=gate,
            llm_conflict_judge=llm_conflict_judge,
            tol_hz=tol_hz,
        )
        if llm_result is not None:
            return llm_result

    if vel_result and vel_result.freq_hz is not None:
        evidence.append(f"速度谱单独识别={vel_hz:.4f}Hz, conf={vel_result.confidence:.3f}")
    if acc_result and acc_result.freq_hz is not None:
        evidence.append(f"加速度谱单独识别={acc_hz:.4f}Hz, conf={acc_result.confidence:.3f}")

    # 未提供 LLM 或 LLM 不可用时的兜底：
    vel_top_freqs = np.asarray((vel_result.debug or {}).get("top_freqs", []), dtype=float) if vel_result else np.array(
        [])
    vel_top_amps = np.asarray((vel_result.debug or {}).get("top_amps", []), dtype=float) if vel_result else np.array([])
    acc_top_freqs = np.asarray((acc_result.debug or {}).get("top_freqs", []), dtype=float) if acc_result else np.array(
        [])
    acc_top_amps = np.asarray((acc_result.debug or {}).get("top_amps", []), dtype=float) if acc_result else np.array([])
    main_candidates: List[float] = [vel_hz]
    if abs(acc_hz - vel_hz) > tol_hz:
        main_candidates.append(acc_hz)
    fused_candidates: List[Dict[str, Any]] = []
    for hz in main_candidates:
        vel_eval = _evaluate_candidate_on_spectrum(hz, vel_top_freqs, vel_top_amps, tol_hz) if len(
            vel_top_freqs) else None
        acc_eval = _evaluate_candidate_on_spectrum(hz, acc_top_freqs, acc_top_amps, tol_hz) if len(
            acc_top_freqs) else None

        score = 0.0
        item_evidence: List[str] = []

        if vel_eval is not None:
            score += vel_weight * float(vel_eval["score"])
            if vel_eval["direct_present"]:
                item_evidence.append("速度谱存在 1X 直接命中")
            if vel_eval["support_count"] >= 2:
                item_evidence.append(f"速度谱支持阶次={vel_eval['support_orders']}")
        if acc_eval is not None:
            score += acc_weight * float(acc_eval["score"])
            if acc_eval["direct_present"]:
                item_evidence.append("加速度谱存在 1X 直接命中")
            if acc_eval["support_count"] >= 2:
                item_evidence.append(f"加速度谱支持阶次={acc_eval['support_orders']}")

        if abs(hz - vel_hz) <= tol_hz:
            score += vel_weight * float(vel_result.confidence if vel_result else 0.0)
            item_evidence.append("该候选即速度谱主结果")
        if abs(hz - acc_hz) <= tol_hz:
            score += acc_weight * float(acc_result.confidence if acc_result else 0.0)
            item_evidence.append("该候选即加速度谱主结果")

        if abs(vel_hz - acc_hz) <= tol_hz and abs(hz - vel_hz) <= tol_hz:
            score += 0.18
            item_evidence.append("速度谱与加速度谱主结果一致")
        elif abs(vel_hz - acc_hz) > tol_hz:
            if abs(hz - vel_hz) <= tol_hz:
                item_evidence.append("当前在主结果分歧下偏向速度谱候选")
            if abs(hz - acc_hz) <= tol_hz:
                item_evidence.append("当前在主结果分歧下偏向加速度谱候选")

        score = float(min(0.99, score / max(2.0 * (vel_weight + acc_weight), 1e-8)))
        fused_candidates.append(
            {
                "freq_hz": round(float(hz), 6),
                "rpm": round(float(hz) * 60.0, 6),
                "score": round(score, 6),
                "velocity_eval": vel_eval,
                "acceleration_eval": acc_eval,
                "evidence": item_evidence,
            }
        )
    fused_candidates.sort(
        key=lambda x: (
            x["score"],
            1 if abs(float(x["freq_hz"]) - vel_hz) <= tol_hz else 0,
            ((x["velocity_eval"] or {}).get("support_count", 0) + (x["acceleration_eval"] or {}).get("support_count",
                                                                                                     0)),
            (1 if (x["velocity_eval"] or {}).get("direct_present") else 0) + (
                1 if (x["acceleration_eval"] or {}).get("direct_present") else 0),
        ),
        reverse=True,
    )
    best = fused_candidates[0]
    best_hz = float(best["freq_hz"])
    best_score = float(best["score"])
    evidence.extend(best.get("evidence", []))

    if abs(vel_hz - acc_hz) > tol_hz:
        conflicts.append(f"双谱存在分歧：速度谱={vel_hz:.4f}Hz，加速度谱={acc_hz:.4f}Hz")

    if best_score < 0.55:
        conflicts.append("双谱联合分数偏低，当前候选证据有限")

    reason = f"双谱主结果二选一：最终转频={best_hz:.4f}Hz，融合得分={best_score:.4f}"
    return MechanicSpeedResult(
        freq_hz=best_hz,
        rpm=best_hz * 60.0,
        confidence=round(best_score, 4),
        supported=best_score >= 0.55,
        reason=reason,
        evidence=evidence,
        conflicts=conflicts,
        top_candidates=fused_candidates[:2],
        debug={
            "fusion_mode": "velocity_acceleration_main_result_only",
            "main_candidates": [round(float(x), 6) for x in main_candidates],
            "velocity_result": vel_result.to_dict() if vel_result else None,
            "acceleration_result": acc_result.to_dict() if acc_result else None,
        },
    )


# =========================
# 对外主函数：仅保留 mechanic_rule_detect_speed
# =========================

def mechanic_rule_detect_speed(
        acc_data: Optional[np.ndarray] = None,
        vel_data: Optional[np.ndarray] = None,
        fs: Optional[float] = None,
        num_fre: int = 10,
        min_amp: float = 0.0,
        tol_hz: float = 1.0,
        bandpass_low_hz: float = 5.0,
        bandpass_high_hz: float = 215.0,
        strong_peak_ratio: float = 5.0,
        remove_close_distance_hz: float = 3.0,
        min_candidate_hz: Optional[float] = 9,
        max_candidate_hz: Optional[float] = 100,
        vel_weight: float = 1.35,
        acc_weight: float = 0.75,
        derive_velocity_from_acc: bool = False,
        llm_conflict_judge: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        plot_enabled: bool = False,
        plot_freq_min_hz: float = 5.0,
        plot_freq_max_hz: float = 215.0,
        plot_title: str = "速度频谱和加速度频谱",
        plot_save_path: Optional[str] = None,
        plot_show: bool = False,
) -> MechanicSpeedResult:
    """
    仅保留 mechanic_rule_detect_speed 及其直接依赖。

    说明：
    - 若未提供 vel_data，默认由加速度积分得到速度；
    - 保持原来的速度优先门控，仅当速度谱命中复核条件时才进入加速度谱判断；
    - 当进入加速度判断后，若加速度与速度主结果不一致，则把信息给到大模型做二选一仲裁；
    - 支持绘制速度频谱和加速度频谱（5~215Hz）并提供保存/关闭按钮。
    """
    if fs is None:
        raise ValueError("fs 不能为空")

    acc_arr = _safe_array(acc_data)
    vel_arr = _safe_array(vel_data)

    if vel_arr is None and derive_velocity_from_acc and acc_arr is not None:
        vel_arr, _ = acc2dis(acc_arr, float(fs))

    if acc_arr is None and vel_arr is None:
        return MechanicSpeedResult(
            freq_hz=None,
            rpm=None,
            confidence=0.0,
            supported=False,
            reason="机理模型无法识别：加速度和速度数据都为空",
            evidence=[],
            conflicts=["acc_data 与 vel_data 均为空"],
            top_candidates=[],
            debug={},
        )

    vel_result: Optional[MechanicSpeedResult] = None
    acc_result: Optional[MechanicSpeedResult] = None

    if vel_arr is not None:
        vel_result = _mechanic_rule_detect_speed_single(
            data=_normalize_signal(vel_arr),
            fs=float(fs),
            spectrum_name="velocity",
            num_fre=num_fre,
            min_amp=min_amp,
            tol_hz=tol_hz,
            bandpass_low_hz=bandpass_low_hz,
            bandpass_high_hz=bandpass_high_hz,
            strong_peak_ratio=strong_peak_ratio,
            remove_close_distance_hz=remove_close_distance_hz,
            min_candidate_hz=min_candidate_hz,
            max_candidate_hz=max_candidate_hz,
        )
    if vel_result is not None:
        gate = _should_trigger_acc_review(vel_result, tol_hz=max(tol_hz, 0.5), low_freq_hz=15.0, high_freq_hz=35.0)

        # 不触发加速度复核时，直接采用速度谱
        if (acc_arr is None) or (not gate["triggered"]):
            if plot_enabled:
                _plot_dual_spectra_with_buttons(
                    vel_result=vel_result,
                    acc_result=None,
                    freq_min_hz=plot_freq_min_hz,
                    freq_max_hz=plot_freq_max_hz,
                    title=plot_title,
                    save_path=plot_save_path,
                    show=plot_show,
                )
            gated_debug = dict(vel_result.debug or {})
            gated_debug.update({
                "fusion_mode": "velocity_only_gate_not_triggered",
                "acc_review_gate": gate,
            })
            return MechanicSpeedResult(
                freq_hz=vel_result.freq_hz,
                rpm=vel_result.rpm,
                confidence=vel_result.confidence,
                supported=vel_result.supported,
                reason=vel_result.reason,
                evidence=list(vel_result.evidence or []) + ["速度谱未命中加速度复核门控，本次直接采用速度谱结果"],
                conflicts=list(vel_result.conflicts or []),
                top_candidates=list(vel_result.top_candidates or []),
                debug=gated_debug,
            )

        # 触发加速度复核
        acc_result = _mechanic_rule_detect_speed_single(
            data=_normalize_signal(acc_arr),
            fs=float(fs),
            spectrum_name="acceleration",
            num_fre=num_fre,
            min_amp=min_amp,
            tol_hz=tol_hz,
            bandpass_low_hz=bandpass_low_hz,
            bandpass_high_hz=bandpass_high_hz,
            strong_peak_ratio=strong_peak_ratio,
            remove_close_distance_hz=remove_close_distance_hz,
            min_candidate_hz=min_candidate_hz,
            max_candidate_hz=max_candidate_hz,
        )
        if plot_enabled:
            _plot_dual_spectra_with_buttons(
                vel_result=vel_result,
                acc_result=acc_result,
                freq_min_hz=plot_freq_min_hz,
                freq_max_hz=plot_freq_max_hz,
                title=plot_title,
                save_path=plot_save_path,
                show=plot_show,
            )

        fused = _fuse_dual_spectrum_results(
            vel_result=vel_result,
            acc_result=acc_result,
            tol_hz=tol_hz,
            vel_weight=vel_weight,
            acc_weight=acc_weight,
            gate=gate,
            llm_conflict_judge=llm_conflict_judge,
        )
        fused.evidence = list(gate["reasons"]) + list(fused.evidence or [])
        fused.debug = dict(fused.debug or {})
        fused.debug.update({
            "fusion_mode": fused.debug.get("fusion_mode", "velocity_gate_then_acc_review"),
            "acc_review_gate": gate,
        })
        return fused

    # 没有速度谱时，退化到仅加速度谱
    if acc_arr is not None:
        acc_result = _mechanic_rule_detect_speed_single(
            data=_normalize_signal(acc_arr),
            fs=float(fs),
            spectrum_name="acceleration",
            num_fre=num_fre,
            min_amp=min_amp,
            tol_hz=tol_hz,
            bandpass_low_hz=bandpass_low_hz,
            bandpass_high_hz=bandpass_high_hz,
            strong_peak_ratio=strong_peak_ratio,
            remove_close_distance_hz=remove_close_distance_hz,
            min_candidate_hz=min_candidate_hz,
            max_candidate_hz=max_candidate_hz,
        )
        if plot_enabled:
            _plot_dual_spectra_with_buttons(
                vel_result=None,
                acc_result=acc_result,
                freq_min_hz=plot_freq_min_hz,
                freq_max_hz=plot_freq_max_hz,
                title=plot_title,
                save_path=plot_save_path,
                show=plot_show,
            )
        acc_debug = dict(acc_result.debug or {})
        acc_debug.update({"fusion_mode": "acceleration_only_no_velocity"})
        return MechanicSpeedResult(
            freq_hz=acc_result.freq_hz,
            rpm=acc_result.rpm,
            confidence=acc_result.confidence,
            supported=acc_result.supported,
            reason=acc_result.reason,
            evidence=["未提供速度谱，退化为仅加速度谱识别"] + list(acc_result.evidence or []),
            conflicts=list(acc_result.conflicts or []),
            top_candidates=list(acc_result.top_candidates or []),
            debug=acc_debug,
        )

    return MechanicSpeedResult(
        freq_hz=None,
        rpm=None,
        confidence=0.0,
        supported=False,
        reason="机理模型无法识别",
        evidence=[],
        conflicts=["双谱识别失败"],
        top_candidates=[],
        debug={},
    )
