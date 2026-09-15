from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.attachments.contracts import AttachmentDescriptor, AttachmentKind
from app.config import Settings
from app.domain.exceptions import AppError
from app.reasoning.contracts import ModelProfile


@dataclass(frozen=True, slots=True)
class ModelStreamDelta:
    channel: str
    content: str
    reasoning_kind: str | None = None


@dataclass(frozen=True, slots=True)
class MultimodalInput:
    descriptor: AttachmentDescriptor
    data: bytes


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolPlanningResult:
    content: str
    tool_calls: list[ModelToolCall]


class OpenAICompatibleChatClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    @staticmethod
    def _headers(profile: ModelProfile) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {profile.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _ensure_profile(profile: ModelProfile) -> None:
        if not profile.enabled or not profile.base_url or not profile.api_key or not profile.model:
            raise AppError(
                "MODEL_PROFILE_NOT_CONFIGURED",
                f"模型配置 {profile.profile_id} 未完整配置",
                503,
            )

    @staticmethod
    def _extract_response_text(
        payload: dict[str, Any], *, invalid_code: str = "MODEL_RESPONSE_INVALID"
    ) -> str:
        if payload.get("output_text"):
            return str(payload["output_text"])
        texts: list[str] = []
        for output in payload.get("output") or []:
            if not isinstance(output, dict):
                continue
            for part in output.get("content") or []:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if text:
                    texts.append(str(text))
        if texts:
            return "\n".join(texts)
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = choices[0].get("message", {}).get("content")
            if content:
                return str(content)
        raise AppError(invalid_code, "模型未返回正文", 502)


    @staticmethod
    def _extract_stream_delta(payload: dict[str, Any]) -> str:
        """Extract public answer text from one OpenAI-compatible stream event.

        Explicit reasoning channels such as ``reasoning_content`` are deliberately
        ignored. Providers that embed reasoning inside ``content`` using
        ``<think>...</think>`` are handled by the incremental think-tag filter at
        the caller boundary before anything is published to SSE.
        """

        event_type = str(payload.get("type") or "")
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            return str(delta) if delta is not None else ""

        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            delta_obj = choices[0].get("delta") or {}
            if isinstance(delta_obj, dict):
                content = delta_obj.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        text = item.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                    return "".join(parts)

        # A few Responses-compatible gateways omit ``type`` but still return a
        # top-level textual delta. Do not accept dict/list deltas because those may
        # contain reasoning metadata rather than final answer text.
        delta = payload.get("delta")
        return str(delta) if isinstance(delta, str) else ""

    def _adapt_reasoning_payload(self, payload, profile):
        """Do not impose Qwen vendor fields on unrelated model APIs."""
        data = dict(payload)
        policy = getattr(self.settings, "model_reasoning_parameters", "auto")
        qwen = str(profile.model or "").lower().startswith(("qwen", "qwq"))
        if policy == "omit" or (policy == "auto" and not qwen):
            data.pop("enable_thinking", None)
            # Responses has its own standard reasoning object; do not translate it.
            data.pop("reasoning_effort", None)
        if not profile.supports_reasoning:
            data.pop("reasoning_effort", None)
            data.pop("reasoning", None)
        if policy == "omit":
            data.pop("reasoning", None)
        return data

    @classmethod
    def _extract_stream_events(cls, payload):
        kind = str(payload.get("type") or "")
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            text = payload.get("delta")
            return [ModelStreamDelta("reasoning", text,
                "summary" if "summary" in kind else "provider_output")] if isinstance(text, str) and text else []
        if kind.startswith("response.") and kind != "response.output_text.delta":
            return []
        result = []
        choices = payload.get("choices") or []
        delta = choices[0].get("delta", {}) if choices and isinstance(choices[0], dict) else {}
        if isinstance(delta, dict):
            # Only explicit, public provider output. Never decode tool arguments or hidden tokens.
            for key in ("reasoning_content", "reasoning"):
                value = delta.get(key)
                if isinstance(value, str) and value:
                    result.append(ModelStreamDelta("reasoning", value, "provider_output"))
                    break
        text = cls._extract_stream_delta(payload)
        if text:
            result.append(ModelStreamDelta("final", text))
        return result

    async def stream_profile(self, **kwargs):
        """Compatibility API for internal callers that only need the final channel."""
        async for item in self.stream_profile_events(**kwargs):
            if item.channel == "final":
                yield item.content

    async def stream_profile_events(
        self,
        *,
        profile: ModelProfile,
        system: str,
        user: str,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[ModelStreamDelta]:
        """Stream provider-exposed reasoning and answer chunks as separate typed channels."""

        self._ensure_profile(profile)
        timeout = timeout_seconds or self.settings.supervisor_timeout_seconds
        if profile.supports_reasoning and self.settings.reasoning_api_mode == "responses":
            url = f"{profile.base_url.rstrip('/')}/responses"
            request_payload: dict[str, Any] = {
                "model": profile.model,
                "instructions": system,
                "input": [{"role": "user", "content": user}],
                "store": False,
                "stream": True,
            }
            if reasoning_effort:
                request_payload["reasoning"] = {"effort": reasoning_effort}
        else:
            url = f"{profile.base_url.rstrip('/')}/chat/completions"
            request_payload = {
                "model": profile.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
                "stream": True,
                "enable_thinking": bool(profile.supports_reasoning),
            }
            if reasoning_effort:
                request_payload["reasoning_effort"] = reasoning_effort

        try:
            async with self.client.stream(
                "POST",
                url,
                headers=self._headers(profile),
                json=self._adapt_reasoning_payload(request_payload, profile),
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    value = str(line or "").strip()
                    if not value or value.startswith(":") or value.startswith("event:"):
                        continue
                    if value.startswith("data:"):
                        value = value[5:].strip()
                    if not value or value == "[DONE]":
                        continue
                    try:
                        event_payload = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event_payload, dict):
                        continue
                    for delta in self._extract_stream_events(event_payload):
                        yield delta
        except httpx.TimeoutException as exc:
            raise AppError("MODEL_TIMEOUT", f"模型 {profile.profile_id} 流式调用超时", 504) from exc
        except httpx.HTTPStatusError as exc:
            detail = (await exc.response.aread()).decode("utf-8", errors="replace")[:700]
            raise AppError(
                "MODEL_UNAVAILABLE",
                f"模型 {profile.profile_id} 流式调用失败: HTTP {exc.response.status_code} {detail}",
                502,
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                "MODEL_UNAVAILABLE", f"模型 {profile.profile_id} 流式调用失败: {exc}", 502
            ) from exc

    async def complete_profile(
        self,
        *,
        profile: ModelProfile,
        system: str,
        user: str,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        self._ensure_profile(profile)
        timeout = timeout_seconds or self.settings.supervisor_timeout_seconds
        try:
            if profile.supports_reasoning and self.settings.reasoning_api_mode == "responses":
                payload: dict[str, Any] = {
                    "model": profile.model,
                    "instructions": system,
                    "input": [{"role": "user", "content": user}],
                    "store": False,
                }
                if reasoning_effort:
                    payload["reasoning"] = {"effort": reasoning_effort}
                response = await self.client.post(
                    f"{profile.base_url.rstrip('/')}/responses",
                    headers=self._headers(profile),
                    json=self._adapt_reasoning_payload(payload, profile),
                    timeout=timeout,
                )
            else:
                payload = {
                    "model": profile.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.1,
                    "stream": False,
                }
                # DashScope/Qwen and several OpenAI-compatible providers support this
                # parameter. Unknown fields are ignored by providers that permit extras.
                payload["enable_thinking"] = bool(profile.supports_reasoning)
                if reasoning_effort:
                    payload["reasoning_effort"] = reasoning_effort
                response = await self.client.post(
                    f"{profile.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(profile),
                    json=self._adapt_reasoning_payload(payload, profile),
                    timeout=timeout,
                )
            response.raise_for_status()
            return self._extract_response_text(response.json())
        except httpx.TimeoutException as exc:
            raise AppError("MODEL_TIMEOUT", f"模型 {profile.profile_id} 调用超时", 504) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:700]
            raise AppError(
                "MODEL_UNAVAILABLE",
                f"模型 {profile.profile_id} 调用失败: HTTP {exc.response.status_code} {detail}",
                502,
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                "MODEL_UNAVAILABLE", f"模型 {profile.profile_id} 调用失败: {exc}", 502
            ) from exc

    async def complete_json_profile(
        self,
        *,
        profile: ModelProfile,
        system: str,
        user: str,
        timeout_seconds: float | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        text = await self.complete_profile(
            profile=profile,
            system=system + "\n只输出一个合法 JSON 对象，不要使用 Markdown 代码块。",
            user=user,
            timeout_seconds=timeout_seconds,
            reasoning_effort=reasoning_effort,
        )
        return self._parse_json_object(text)

    async def plan_tool_calls(
        self,
        *,
        profile: ModelProfile,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        timeout_seconds: float | None = None,
        force_single_call: bool = False,
    ) -> ToolPlanningResult:
        """Use OpenAI-compatible Function Calling for Normal/Expert supervisors.

        Some reasoning providers only support the Responses endpoint. For those providers,
        or when a compatible endpoint rejects tool calls, the method falls back to a strict
        JSON action contract without changing the graph semantics.
        """
        self._ensure_profile(profile)
        timeout = timeout_seconds or self.settings.supervisor_timeout_seconds
        try:
            response = await self.client.post(
                f"{profile.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(profile),
                json=self._adapt_reasoning_payload({
                    "model": profile.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": not force_single_call,
                    "enable_thinking": bool(profile.supports_reasoning),
                    "temperature": 0.0,
                    "stream": False,
                }, profile),
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            calls: list[ModelToolCall] = []
            for item in message.get("tool_calls") or []:
                function = item.get("function") or {}
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
                except (TypeError, ValueError, json.JSONDecodeError):
                    arguments = {"objective": str(raw_arguments)}
                calls.append(
                    ModelToolCall(
                        call_id=str(item.get("id") or f"call_{len(calls) + 1}"),
                        name=str(function.get("name") or ""),
                        arguments=arguments,
                    )
                )
            return ToolPlanningResult(content=str(message.get("content") or ""), tool_calls=calls)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            schema = [
                {
                    "name": item.get("function", {}).get("name"),
                    "description": item.get("function", {}).get("description"),
                    "parameters": item.get("function", {}).get("parameters") or {},
                }
                for item in tools
            ]
            fallback = await self.complete_json_profile(
                profile=profile,
                system=system,
                user=(
                    user
                    + "\n\n可用函数："
                    + json.dumps(schema, ensure_ascii=False)
                    + "\n返回格式：{\"calls\":[{\"name\":\"函数名\",\"arguments\":{\"objective\":\"目标\"}}],\"content\":\"可选说明\"}。"
                ),
                timeout_seconds=timeout,
            )
            calls = []
            for index, item in enumerate(fallback.get("calls") or [], start=1):
                if not isinstance(item, dict):
                    continue
                calls.append(
                    ModelToolCall(
                        call_id=f"call_{index}",
                        name=str(item.get("name") or ""),
                        arguments=dict(item.get("arguments") or {}),
                    )
                )
            if force_single_call:
                calls = calls[:1]
            return ToolPlanningResult(content=str(fallback.get("content") or ""), tool_calls=calls)

    async def stream_multimodal_profile(self, **kwargs):
        async for item in self.stream_multimodal_profile_events(**kwargs):
            if item.channel == "final":
                yield item.content

    async def stream_multimodal_profile_events(
        self,
        *,
        profile: ModelProfile,
        system: str,
        user: str,
        attachments: list[MultimodalInput],
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[ModelStreamDelta]:
        """Stream image/file analysis using the same native payload as the blocking path.

        Images use OpenAI-compatible ``chat/completions`` when possible. PDF and
        other native files use the Responses API, because that is the endpoint
        accepted by the configured providers for ``input_file`` blocks. Explicit
        reasoning channels remain separate; inline think tags are split by the caller.
        """

        self._ensure_profile(profile)
        if attachments and not (profile.supports_files or profile.supports_vision):
            raise AppError(
                "MODEL_ATTACHMENT_UNSUPPORTED",
                f"模型配置 {profile.profile_id} 不支持附件",
                422,
            )
        timeout = timeout_seconds or self.settings.multimodal_timeout_seconds
        use_responses_api = (
            self.settings.reasoning_api_mode == "responses"
            or any(item.descriptor.kind != AttachmentKind.IMAGE for item in attachments)
        )

        if use_responses_api:
            content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
            for item in attachments:
                encoded = base64.b64encode(item.data).decode("ascii")
                data_url = f"data:{item.descriptor.mime_type};base64,{encoded}"
                if item.descriptor.kind == AttachmentKind.IMAGE:
                    content.append(
                        {"type": "input_image", "image_url": data_url, "detail": "high"}
                    )
                else:
                    block: dict[str, Any] = {
                        "type": "input_file",
                        "filename": item.descriptor.filename,
                        "file_data": data_url,
                    }
                    if item.descriptor.kind == AttachmentKind.PDF:
                        block["detail"] = self.settings.multimodal_pdf_detail
                    content.append(block)
            payload: dict[str, Any] = {
                "model": profile.model,
                "instructions": system,
                "input": [{"role": "user", "content": content}],
                "store": False,
                "stream": True,
            }
            if profile.supports_reasoning and reasoning_effort:
                payload["reasoning"] = {"effort": reasoning_effort}
            url = f"{profile.base_url.rstrip('/')}/responses"
        else:
            chat_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
            for item in attachments:
                if item.descriptor.kind != AttachmentKind.IMAGE:
                    continue
                encoded = base64.b64encode(item.data).decode("ascii")
                data_url = f"data:{item.descriptor.mime_type};base64,{encoded}"
                chat_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    }
                )
            payload = {
                "model": profile.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": chat_content},
                ],
                "enable_thinking": bool(profile.supports_reasoning),
                "stream": True,
                "temperature": 0.1,
            }
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            url = f"{profile.base_url.rstrip('/')}/chat/completions"

        try:
            async with self.client.stream(
                "POST",
                url,
                headers=self._headers(profile),
                json=self._adapt_reasoning_payload(payload, profile),
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    value = str(line or "").strip()
                    if not value or value.startswith(":") or value.startswith("event:"):
                        continue
                    if value.startswith("data:"):
                        value = value[5:].strip()
                    if not value or value == "[DONE]":
                        continue
                    try:
                        event_payload = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event_payload, dict):
                        continue
                    for delta in self._extract_stream_events(event_payload):
                        yield delta
        except httpx.TimeoutException as exc:
            raise AppError(
                "MULTIMODAL_MODEL_TIMEOUT", "图片或文件流式分析超时", 504
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = (await exc.response.aread()).decode(
                "utf-8", errors="replace"
            )[:700]
            raise AppError(
                "MULTIMODAL_MODEL_UNAVAILABLE",
                f"多模态模型流式调用失败: HTTP {exc.response.status_code} {detail}",
                502,
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                "MULTIMODAL_MODEL_UNAVAILABLE",
                f"多模态模型流式调用失败: {exc}",
                502,
            ) from exc

    async def complete_multimodal_profile(
        self,
        *,
        profile: ModelProfile,
        system: str,
        user: str,
        attachments: list[MultimodalInput],
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        self._ensure_profile(profile)
        if attachments and not (profile.supports_files or profile.supports_vision):
            raise AppError(
                "MODEL_ATTACHMENT_UNSUPPORTED",
                f"模型配置 {profile.profile_id} 不支持附件",
                422,
            )
        timeout = timeout_seconds or self.settings.multimodal_timeout_seconds
        try:
            use_responses_api = (
                self.settings.reasoning_api_mode == "responses"
                or any(
                    item.descriptor.kind != AttachmentKind.IMAGE
                    for item in attachments
                )
            )
            if use_responses_api:
                content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
                for item in attachments:
                    encoded = base64.b64encode(item.data).decode("ascii")
                    data_url = f"data:{item.descriptor.mime_type};base64,{encoded}"
                    if item.descriptor.kind == AttachmentKind.IMAGE:
                        content.append(
                            {"type": "input_image", "image_url": data_url, "detail": "high"}
                        )
                    else:
                        block: dict[str, Any] = {
                            "type": "input_file",
                            "filename": item.descriptor.filename,
                            "file_data": data_url,
                        }
                        if item.descriptor.kind == AttachmentKind.PDF:
                            block["detail"] = self.settings.multimodal_pdf_detail
                        content.append(block)
                payload: dict[str, Any] = {
                    "model": profile.model,
                    "instructions": system,
                    "input": [{"role": "user", "content": content}],
                    "store": False,
                }
                if profile.supports_reasoning and reasoning_effort:
                    payload["reasoning"] = {"effort": reasoning_effort}
                response = await self.client.post(
                    f"{profile.base_url.rstrip('/')}/responses",
                    headers=self._headers(profile),
                    json=self._adapt_reasoning_payload(payload, profile),
                    timeout=timeout,
                )
            else:
                chat_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
                for item in attachments:
                    if item.descriptor.kind != AttachmentKind.IMAGE:
                        continue
                    encoded = base64.b64encode(item.data).decode("ascii")
                    data_url = f"data:{item.descriptor.mime_type};base64,{encoded}"
                    chat_content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        }
                    )
                payload = {
                    "model": profile.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": chat_content},
                    ],
                    "enable_thinking": bool(profile.supports_reasoning),
                    "stream": False,
                    "temperature": 0.1,
                }
                if reasoning_effort:
                    payload["reasoning_effort"] = reasoning_effort
                response = await self.client.post(
                    f"{profile.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(profile),
                    json=self._adapt_reasoning_payload(payload, profile),
                    timeout=timeout,
                )
            response.raise_for_status()
            return self._extract_response_text(
                response.json(), invalid_code="MULTIMODAL_MODEL_INVALID"
            )
        except httpx.TimeoutException as exc:
            raise AppError("MULTIMODAL_MODEL_TIMEOUT", "图片或文件分析超时", 504) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:700]
            raise AppError(
                "MULTIMODAL_MODEL_UNAVAILABLE",
                f"多模态模型调用失败: HTTP {exc.response.status_code} {detail}",
                502,
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                "MULTIMODAL_MODEL_UNAVAILABLE", f"多模态模型调用失败: {exc}", 502
            ) from exc

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        value = text.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*", "", value)
            value = re.sub(r"\s*```$", "", value)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            start = value.find("{")
            end = value.rfind("}")
            if start < 0 or end <= start:
                raise AppError("MODEL_JSON_INVALID", "模型未返回合法 JSON 对象", 502)
            try:
                payload = json.loads(value[start : end + 1])
            except json.JSONDecodeError as exc:
                raise AppError("MODEL_JSON_INVALID", "模型返回的 JSON 无法解析", 502) from exc
        if not isinstance(payload, dict):
            raise AppError("MODEL_JSON_INVALID", "模型 JSON 顶层必须是对象", 502)
        return payload
