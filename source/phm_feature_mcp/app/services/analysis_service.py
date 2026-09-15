from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

import numpy as np

from app.config import Settings
from app.core.signal_codec import validate_and_sanitize_signal
from app.core.vibration_features import extract_feature_groups
from app.engine import SpeedInferenceEngine, SpeedJob
from app.schemas import FeatureOptions, FeatureResponse, RotationalSpeedFeatureResponse, VibrationSignalInput

T = TypeVar("T")


class AnalysisService:
    """Concurrency and lifecycle boundary for CPU-bound feature extraction."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.speed_engine = SpeedInferenceEngine(settings)
        self.executor = ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix="phm-feature")
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.speed_engine.close()
        self.executor.shutdown(wait=True, cancel_futures=False)

    async def _run(self, func: Callable[..., T], *args) -> T:
        if self._closed:
            raise RuntimeError("analysis service is shutting down")
        acquired = False
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=self.settings.queue_wait_timeout_s)
            acquired = True
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"analysis queue wait exceeded {self.settings.queue_wait_timeout_s:.3f}s") from exc

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self.executor, func, *args)
        finally:
            if acquired:
                self.semaphore.release()

    async def extract_rotational_speed_feature(self, job: SpeedJob) -> RotationalSpeedFeatureResponse:
        return await self._run(self.speed_engine.run, job)

    async def extract_features(
        self,
        signal_input: VibrationSignalInput,
        x: np.ndarray,
        options: FeatureOptions,
    ) -> FeatureResponse:
        return await self._run(self._extract_features_sync, signal_input, x, options)

    def _extract_features_sync(
        self,
        signal_input: VibrationSignalInput,
        x: np.ndarray,
        options: FeatureOptions,
    ) -> FeatureResponse:
        start = time.perf_counter()
        clean, quality = validate_and_sanitize_signal(
            x,
            fs_hz=signal_input.fs_hz,
            min_samples=self.settings.min_samples_per_request,
            max_samples=self.settings.max_samples_per_request,
            min_finite_ratio=self.settings.min_finite_ratio,
        )

        time_domain, frequency_domain, envelope_domain, band_energy = extract_feature_groups(
            clean,
            signal_input.fs_hz,
            feature_set=options.feature_set,
            detrend=options.detrend,
            window=options.window,
            bands=options.frequency_bands,
            envelope_band=options.envelope_band,
        )

        rounded_groups = []
        for group in (time_domain, frequency_domain, envelope_domain, band_energy):
            rounded_groups.append({k: round(float(v), 10) for k, v in group.items()})
        time_domain_r, frequency_domain_r, envelope_domain_r, band_energy_r = rounded_groups
        feature_count = sum(len(group) for group in rounded_groups)

        return FeatureResponse(
            feature_set=options.feature_set,
            signal_type=signal_input.signal_type,
            unit=signal_input.unit,
            fs_hz=signal_input.fs_hz,
            device_info=signal_input.device_info,
            data_quality=quality,
            time_domain=time_domain_r,
            frequency_domain=frequency_domain_r,
            envelope_domain=envelope_domain_r,
            band_energy=band_energy_r,
            feature_count=feature_count,
            elapsed_ms=round((time.perf_counter() - start) * 1000.0, 3),
        )
