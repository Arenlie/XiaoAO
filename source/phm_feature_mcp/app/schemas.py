from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


SignalType = Literal["acceleration", "velocity", "displacement"]
FeatureSet = Literal["basic", "standard", "bearing", "full"]


class SignalPayload(StrictModel):
    """One waveform payload. Use exactly one representation."""

    samples: Optional[List[float]] = Field(
        default=None,
        description="Waveform samples as JSON numbers. Prefer float32_base64 for large waveforms.",
    )
    float32_base64: Optional[str] = Field(
        default=None,
        description="Little-endian float32 waveform bytes encoded with base64.",
    )

    @model_validator(mode="after")
    def exactly_one_payload(self) -> "SignalPayload":
        provided = int(self.samples is not None) + int(self.float32_base64 is not None)
        if provided != 1:
            raise ValueError("exactly one of samples or float32_base64 must be provided")
        if self.samples is not None and len(self.samples) == 0:
            raise ValueError("samples cannot be empty")
        if self.float32_base64 is not None and not self.float32_base64.strip():
            raise ValueError("float32_base64 cannot be empty")
        return self


class DeviceInfo(StrictModel):
    device_code: Optional[str] = None
    device_name: Optional[str] = None
    point_code: Optional[str] = None
    point_name: Optional[str] = None
    location: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class VibrationSignalInput(StrictModel):
    fs_hz: float = Field(..., gt=0.0, description="Sampling frequency in Hz")
    signal_type: SignalType = "acceleration"
    unit: Optional[str] = None
    data: SignalPayload
    device_info: Optional[DeviceInfo] = None


class DataQuality(StrictModel):
    n_samples: int
    finite_ratio: float
    duration_s: float
    df_hz: float
    min_value: Optional[float] = None
    max_value: Optional[float] = None


class FrequencyBand(StrictModel):
    name: str = Field(..., min_length=1, max_length=64)
    low_hz: float = Field(..., ge=0.0)
    high_hz: float = Field(..., gt=0.0)

    @model_validator(mode="after")
    def validate_band(self) -> "FrequencyBand":
        if self.high_hz <= self.low_hz:
            raise ValueError("high_hz must be greater than low_hz")
        return self


class EnvelopeBand(StrictModel):
    """Optional resonance band used internally before Hilbert-envelope feature extraction."""

    low_hz: float = Field(..., gt=0.0)
    high_hz: float = Field(..., gt=0.0)
    filter_order: int = Field(default=4, ge=1, le=10)

    @model_validator(mode="after")
    def validate_band(self) -> "EnvelopeBand":
        if self.high_hz <= self.low_hz:
            raise ValueError("high_hz must be greater than low_hz")
        return self


class FeatureOptions(StrictModel):
    """Feature-extraction policy. Signal processing remains internal to this MCP."""

    feature_set: FeatureSet = "standard"
    detrend: bool = True
    window: str = "hann"
    frequency_bands: List[FrequencyBand] = Field(default_factory=list)
    envelope_band: Optional[EnvelopeBand] = None


class FeatureResponse(StrictModel):
    feature_set: FeatureSet
    signal_type: SignalType
    unit: Optional[str] = None
    fs_hz: float
    device_info: Optional[DeviceInfo] = None
    data_quality: DataQuality
    time_domain: Dict[str, float] = Field(default_factory=dict)
    frequency_domain: Dict[str, float] = Field(default_factory=dict)
    envelope_domain: Dict[str, float] = Field(default_factory=dict)
    band_energy: Dict[str, float] = Field(default_factory=dict)
    feature_count: int
    elapsed_ms: float


class AlgorithmConfig(StrictModel):
    """Rotational-speed feature tuning. Defaults retain the validated rule algorithm."""

    num_fre: int = Field(default=10, ge=3, le=30)
    min_amp: float = Field(default=0.0, ge=0.0)
    tol_hz: float = Field(default=1.0, gt=0.0, le=10.0)
    bandpass_low_hz: float = Field(default=5.0, gt=0.0)
    bandpass_high_hz: float = Field(default=215.0, gt=0.0)
    strong_peak_ratio: float = Field(default=5.0, gt=0.0)
    remove_close_distance_hz: float = Field(default=3.0, gt=0.0)
    min_candidate_hz: Optional[float] = Field(default=9.0, gt=0.0)
    max_candidate_hz: Optional[float] = Field(default=100.0, gt=0.0)
    vel_weight: float = Field(default=1.35, ge=0.0)
    acc_weight: float = Field(default=0.75, ge=0.0)
    derive_velocity_from_acc: bool = True
    preprocess_velocity_from_acc: bool = True
    return_debug: bool = False

    @model_validator(mode="after")
    def validate_ranges(self) -> "AlgorithmConfig":
        if self.bandpass_high_hz <= self.bandpass_low_hz:
            raise ValueError("bandpass_high_hz must be greater than bandpass_low_hz")
        if self.min_candidate_hz is not None and self.max_candidate_hz is not None:
            if self.max_candidate_hz <= self.min_candidate_hz:
                raise ValueError("max_candidate_hz must be greater than min_candidate_hz")
        if self.vel_weight + self.acc_weight <= 0:
            raise ValueError("vel_weight + acc_weight must be > 0")
        return self


class RotationalSpeedFeatureResponse(StrictModel):
    request_id: str
    supported: bool
    rotational_frequency_hz: Optional[float]
    rpm: Optional[float]
    confidence: float
    harmonics_hz: Dict[str, Optional[float]] = Field(default_factory=dict)
    reason: str
    evidence: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    top_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    device_info: Optional[DeviceInfo] = None
    data_quality: Optional[DataQuality] = None
    elapsed_ms: float
    algorithm_version: str
    analysis_trace: List[str] = Field(default_factory=list)
    debug: Optional[Dict[str, Any]] = None


class CapabilityResponse(StrictModel):
    service: str
    version: str
    algorithm_version: str
    transport: str
    endpoint: str
    tools: List[str]
    feature_sets: Dict[str, List[str]]
    input_encodings: List[str]
    limits: Dict[str, int]
