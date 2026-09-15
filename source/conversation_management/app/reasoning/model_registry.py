from __future__ import annotations

from app.config import Settings
from app.reasoning.contracts import ModelProfile


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get(self, profile_id: str) -> ModelProfile:
        if profile_id == "expert":
            return ModelProfile(
                profile_id="expert",
                base_url=self.settings.expert_model_base_url or self.settings.supervisor_model_base_url,
                api_key=self.settings.expert_model_api_key or self.settings.supervisor_model_api_key,
                model=self.settings.expert_model or self.settings.supervisor_model,
                supports_vision=True,
                supports_files=True,
                supports_tool_calling=True,
                supports_structured_output=True,
                supports_reasoning=self.settings.supervisor_enable_reasoning,
                speed_rank=30,
                intelligence_rank=100,
            )
        if profile_id == "supervisor":
            return ModelProfile(
                profile_id="supervisor",
                base_url=self.settings.supervisor_model_base_url,
                api_key=self.settings.supervisor_model_api_key,
                model=self.settings.supervisor_model,
                supports_vision=True,
                supports_files=True,
                supports_tool_calling=True,
                supports_structured_output=True,
                supports_reasoning=self.settings.supervisor_enable_reasoning,
                speed_rank=50,
                intelligence_rank=90,
            )
        if profile_id == "quick_multimodal":
            return ModelProfile(
                profile_id="quick_multimodal",
                base_url=self.settings.multimodal_base_url or self.settings.quick_model_base_url or self.settings.supervisor_model_base_url,
                api_key=self.settings.multimodal_api_key or self.settings.quick_model_api_key or self.settings.supervisor_model_api_key,
                model=self.settings.quick_vision_model or self.settings.multimodal_model or self.settings.quick_model or self.settings.supervisor_model,
                supports_vision=True,
                supports_files=True,
                supports_reasoning=self.settings.quick_enable_reasoning,
                speed_rank=80,
                intelligence_rank=70,
            )
        if profile_id == "quick_reasoning":
            return ModelProfile(
                profile_id="quick_reasoning",
                base_url=self.settings.quick_model_base_url or self.settings.supervisor_model_base_url,
                api_key=self.settings.quick_model_api_key or self.settings.supervisor_model_api_key,
                model=self.settings.quick_model or self.settings.supervisor_model,
                supports_vision=True,
                supports_files=True,
                supports_reasoning=True,
                speed_rank=55,
                intelligence_rank=85,
            )
        return ModelProfile(
            profile_id="quick",
            base_url=self.settings.quick_model_base_url,
            api_key=self.settings.quick_model_api_key,
            model=self.settings.quick_model,
            supports_reasoning=False,
            speed_rank=100,
            intelligence_rank=65,
        )

    def quick(self, *, has_attachments: bool = False) -> ModelProfile:
        if has_attachments:
            return self.get("quick_multimodal")
        if self.settings.quick_enable_reasoning:
            return self.get("quick_reasoning")
        return self.get("quick")
