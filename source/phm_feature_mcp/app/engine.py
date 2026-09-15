from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import numpy as np

from app.config import Settings
from app.core.mechanic_rule_model import MechanicSpeedResult, mechanic_rule_detect_speed
from app.core.spectrum_tools import preprocess_signal_for_speed_judgement
from app.infrastructure.llm_judge import OpenAICompatibleConflictJudge
from app.schemas import AlgorithmConfig, DataQuality, DeviceInfo, RotationalSpeedFeatureResponse

logger = logging.getLogger(__name__)


@dataclass
class SpeedJob:
    fs_hz: float
    acceleration: Optional[np.ndarray] = None
    velocity: Optional[np.ndarray] = None
    device_info: Optional[DeviceInfo] = None
    algorithm_config: AlgorithmConfig = field(default_factory=AlgorithmConfig)
    use_llm_judge: Optional[bool] = None
    request_id: Optional[str] = None


@dataclass
class PreparedSignal:
    request_id: str
    acc_arr: Optional[np.ndarray]
    vel_arr: Optional[np.ndarray]
    fs_hz: float
    data_quality: DataQuality
    trace: list[str]


class SpeedInferenceEngine:
    """Pure synchronous RPM inference pipeline. MCP concerns do not live here."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._llm_judge: Optional[OpenAICompatibleConflictJudge] = None
        if settings.enable_llm_judge and settings.llm_base_url and settings.llm_model:
            self._llm_judge = OpenAICompatibleConflictJudge(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key or "",
                model=settings.llm_model,
                timeout_s=settings.llm_timeout_s,
            )

    def close(self) -> None:
        if self._llm_judge is not None:
            self._llm_judge.close()

    def run(self, job: SpeedJob) -> RotationalSpeedFeatureResponse:
        start = time.perf_counter()
        prepared = self.prepare(job)
        result = self.detect(job, prepared)
        return self.build_response(job, prepared, result, (time.perf_counter() - start) * 1000.0)

    def prepare(self, job: SpeedJob) -> PreparedSignal:
        if job.fs_hz <= 0:
            raise ValueError("fs_hz must be > 0")
        if job.acceleration is None and job.velocity is None:
            raise ValueError("acceleration and velocity cannot both be empty")

        request_id = job.request_id or uuid.uuid4().hex
        trace = ["input_validated"]
        acc_arr, acc_q = self._to_array(job.acceleration, "acceleration", job.fs_hz) if job.acceleration is not None else (None, None)
        vel_arr, vel_q = self._to_array(job.velocity, "velocity", job.fs_hz) if job.velocity is not None else (None, None)

        if acc_arr is not None and vel_arr is not None and len(acc_arr) != len(vel_arr):
            raise ValueError(f"acceleration and velocity lengths differ: {len(acc_arr)} != {len(vel_arr)}")

        quality = acc_q if acc_q is not None else vel_q
        assert quality is not None
        cfg = job.algorithm_config
        nyquist = job.fs_hz / 2.0
        if cfg.bandpass_high_hz >= nyquist:
            raise ValueError(
                f"bandpass_high_hz={cfg.bandpass_high_hz} must be below Nyquist frequency {nyquist:.3f}Hz"
            )
        trace.append("data_quality_checked")

        if vel_arr is None and acc_arr is not None and cfg.derive_velocity_from_acc and cfg.preprocess_velocity_from_acc:
            vel_arr = preprocess_signal_for_speed_judgement(
                signal_arr=acc_arr,
                fs_hz=job.fs_hz,
                signal_type="acceleration",
                lowcut_hz=cfg.bandpass_low_hz,
                highcut_hz=cfg.bandpass_high_hz,
            )
            trace.append("velocity_derived_from_acceleration")

        return PreparedSignal(
            request_id=request_id,
            acc_arr=acc_arr,
            vel_arr=vel_arr,
            fs_hz=float(job.fs_hz),
            data_quality=quality,
            trace=trace,
        )

    def detect(self, job: SpeedJob, prepared: PreparedSignal) -> MechanicSpeedResult:
        cfg = job.algorithm_config
        llm_judge = self._select_llm_judge(job)
        prepared.trace.append("mechanic_rule_started")
        result = mechanic_rule_detect_speed(
            acc_data=prepared.acc_arr,
            vel_data=prepared.vel_arr,
            fs=prepared.fs_hz,
            num_fre=cfg.num_fre,
            min_amp=cfg.min_amp,
            tol_hz=cfg.tol_hz,
            bandpass_low_hz=cfg.bandpass_low_hz,
            bandpass_high_hz=cfg.bandpass_high_hz,
            strong_peak_ratio=cfg.strong_peak_ratio,
            remove_close_distance_hz=cfg.remove_close_distance_hz,
            min_candidate_hz=cfg.min_candidate_hz,
            max_candidate_hz=cfg.max_candidate_hz,
            vel_weight=cfg.vel_weight,
            acc_weight=cfg.acc_weight,
            derive_velocity_from_acc=False,
            llm_conflict_judge=llm_judge,
            plot_enabled=False,
            plot_show=False,
        )
        prepared.trace.append("mechanic_rule_completed")
        if llm_judge is not None and result.conflicts:
            prepared.trace.append("llm_conflict_arbitration_available")
        return result

    def _to_array(self, data: np.ndarray, name: str, fs_hz: float) -> tuple[np.ndarray, DataQuality]:
        try:
            raw = np.asarray(data, dtype=np.float64).reshape(-1)
        except Exception as exc:
            raise ValueError(f"{name} cannot be converted to float64: {exc}") from exc

        n = len(raw)
        if n < self.settings.min_samples_per_request:
            raise ValueError(f"waveform too short: {n} samples; minimum is {self.settings.min_samples_per_request}")
        if n > self.settings.max_samples_per_request:
            raise ValueError(f"waveform too long: {n} samples; maximum is {self.settings.max_samples_per_request}")

        finite = np.isfinite(raw)
        finite_ratio = float(np.mean(finite)) if n else 0.0
        if finite_ratio < self.settings.min_finite_ratio:
            raise ValueError(
                f"finite sample ratio is too low: {finite_ratio:.4f}; minimum is {self.settings.min_finite_ratio:.4f}"
            )
        finite_values = raw[finite]
        arr = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)
        if np.allclose(arr, 0.0):
            raise ValueError(f"{name} is all zero after sanitization")

        return arr, DataQuality(
            n_samples=n,
            finite_ratio=round(finite_ratio, 6),
            duration_s=float(n / fs_hz),
            df_hz=float(fs_hz / n),
            min_value=float(np.min(finite_values)) if finite_values.size else None,
            max_value=float(np.max(finite_values)) if finite_values.size else None,
        )

    def _select_llm_judge(self, job: SpeedJob) -> Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]:
        requested = self.settings.enable_llm_judge if job.use_llm_judge is None else job.use_llm_judge
        if not requested:
            return None
        if self._llm_judge is None:
            logger.warning("LLM judge requested but not configured; falling back to rule-only inference")
            return None
        return self._llm_judge

    @staticmethod
    def _round_optional(value: Optional[float], ndigits: int) -> Optional[float]:
        if value is None:
            return None
        try:
            val = float(value)
            return round(val, ndigits) if np.isfinite(val) else None
        except Exception:
            return None

    def build_response(
        self,
        job: SpeedJob,
        prepared: PreparedSignal,
        result: MechanicSpeedResult,
        elapsed_ms: float,
    ) -> RotationalSpeedFeatureResponse:
        return RotationalSpeedFeatureResponse(
            request_id=prepared.request_id,
            supported=bool(result.supported),
            rotational_frequency_hz=self._round_optional(result.freq_hz, 4),
            harmonics_hz={
                "1x": self._round_optional(result.freq_hz, 4),
                "2x": self._round_optional((result.freq_hz * 2.0) if result.freq_hz is not None else None, 4),
                "3x": self._round_optional((result.freq_hz * 3.0) if result.freq_hz is not None else None, 4),
            },
            rpm=self._round_optional(result.rpm, 2),
            confidence=round(float(result.confidence or 0.0), 4),
            reason=str(result.reason or ""),
            evidence=list(result.evidence or []),
            conflicts=list(result.conflicts or []),
            top_candidates=list(result.top_candidates or [])[:10],
            device_info=job.device_info,
            data_quality=prepared.data_quality,
            elapsed_ms=round(elapsed_ms, 3),
            algorithm_version=self.settings.algorithm_version,
            analysis_trace=prepared.trace + ["response_normalized"],
            debug=result.debug if job.algorithm_config.return_debug else None,
        )
