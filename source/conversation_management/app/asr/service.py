from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.domain.exceptions import AppError


@dataclass(frozen=True, slots=True)
class AsrTranscription:
    text: str
    provider: str
    model: str
    language: str | None = None
    emotion: str | None = None
    duration_seconds: float | None = None
    upstream_request_id: str | None = None


class AsrService:
    """Standalone audio-to-text service.

    The public API supports two interchangeable backends:
    - ``dashscope``: Qwen3-ASR-Flash via DashScope synchronous REST API.
    - ``openai_compatible``: an OpenAI-compatible Qwen3-ASR server, suitable
      for a local vLLM deployment.

    No ASR SDK is required; the service reuses the application's shared httpx
    client and sends uploaded audio as a base64 data URL.
    """

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._settings = settings

    async def transcribe(
        self,
        *,
        audio: bytes,
        mime_type: str,
        language: str | None = None,
        context: str | None = None,
        enable_itn: bool | None = None,
    ) -> AsrTranscription:
        if not self._settings.asr_enabled:
            raise AppError("ASR_DISABLED", "语音识别功能未启用", 503)
        if not audio:
            raise AppError("ASR_EMPTY_AUDIO", "上传的音频为空", 400)
        if len(audio) > self._settings.asr_max_file_bytes:
            max_mb = self._settings.asr_max_file_bytes / 1024 / 1024
            raise AppError(
                "ASR_AUDIO_TOO_LARGE",
                f"音频文件过大，当前上限为 {max_mb:g} MB",
                413,
            )

        normalized_mime = self._normalize_mime_type(mime_type)
        selected_language = self._normalize_optional(language)
        if selected_language is None:
            selected_language = self._normalize_optional(self._settings.asr_default_language)
        selected_context = self._normalize_optional(context)
        if selected_context is None:
            selected_context = self._normalize_optional(self._settings.asr_context)
        selected_itn = self._settings.asr_enable_itn if enable_itn is None else enable_itn

        provider = self._settings.asr_provider
        if provider == "dashscope":
            data_url = self._to_data_url(audio, normalized_mime)
            if len(data_url.encode("utf-8")) > 10 * 1024 * 1024:
                raise AppError(
                    "ASR_AUDIO_TOO_LARGE",
                    "音频经 Base64 编码后超过 DashScope 10 MB 输入限制，请缩短或压缩录音",
                    413,
                )
            return await self._transcribe_dashscope(
                data_url=data_url,
                language=selected_language,
                context=selected_context,
                enable_itn=selected_itn,
            )
        if provider == "openai_compatible":
            return await self._transcribe_openai_compatible(
                audio=audio,
                mime_type=normalized_mime,
                language=selected_language,
            )
        raise AppError("ASR_PROVIDER_UNSUPPORTED", f"不支持的 ASR Provider: {provider}", 500)

    async def _transcribe_dashscope(
        self,
        *,
        data_url: str,
        language: str | None,
        context: str | None,
        enable_itn: bool,
    ) -> AsrTranscription:
        api_key = self._require_api_key()
        messages: list[dict[str, Any]] = []
        if context:
            messages.append({"role": "system", "content": [{"text": context}]})
        messages.append({"role": "user", "content": [{"audio": data_url}]})

        asr_options: dict[str, Any] = {"enable_itn": enable_itn}
        if language:
            asr_options["language"] = language

        endpoint = self._dashscope_endpoint(self._settings.asr_base_url)
        payload = {
            "model": self._settings.asr_model,
            "input": {"messages": messages},
            "parameters": {
                "result_format": "message",
                "asr_options": asr_options,
            },
        }
        body = await self._post_json(endpoint, api_key, payload)
        return self._parse_dashscope(body)

    async def _transcribe_openai_compatible(
        self,
        *,
        audio: bytes,
        mime_type: str,
        language: str | None,
    ) -> AsrTranscription:
        api_key = self._settings.asr_api_key or "EMPTY"
        endpoint = f"{self._settings.asr_base_url.rstrip('/')}/audio/transcriptions"
        form: dict[str, str] = {"model": self._settings.asr_model}
        if language:
            form["language"] = language
        try:
            response = await self._http.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                data=form,
                files={"file": ("audio", audio, mime_type)},
                timeout=self._settings.asr_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise AppError("ASR_UPSTREAM_TIMEOUT", "语音识别服务请求超时", 504) from exc
        except httpx.HTTPError as exc:
            raise AppError("ASR_UPSTREAM_UNAVAILABLE", f"语音识别服务不可用: {exc}", 502) from exc

        if response.status_code >= 400:
            detail = self._safe_upstream_error(response)
            raise AppError(
                "ASR_UPSTREAM_ERROR",
                f"语音识别服务返回错误 HTTP {response.status_code}: {detail}",
                502,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AppError("ASR_INVALID_RESPONSE", "语音识别服务返回了无效 JSON", 502) from exc
        if not isinstance(body, dict):
            raise AppError("ASR_INVALID_RESPONSE", "语音识别服务响应格式无效", 502)
        return self._parse_openai_transcription(body)

    async def _post_json(
        self,
        endpoint: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._settings.asr_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise AppError("ASR_UPSTREAM_TIMEOUT", "语音识别服务请求超时", 504) from exc
        except httpx.HTTPError as exc:
            raise AppError("ASR_UPSTREAM_UNAVAILABLE", f"语音识别服务不可用: {exc}", 502) from exc

        if response.status_code >= 400:
            detail = self._safe_upstream_error(response)
            raise AppError(
                "ASR_UPSTREAM_ERROR",
                f"语音识别服务返回错误 HTTP {response.status_code}: {detail}",
                502,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AppError("ASR_INVALID_RESPONSE", "语音识别服务返回了无效 JSON", 502) from exc
        if not isinstance(body, dict):
            raise AppError("ASR_INVALID_RESPONSE", "语音识别服务响应格式无效", 502)
        return body

    def _parse_dashscope(self, body: dict[str, Any]) -> AsrTranscription:
        try:
            message = body["output"]["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("ASR_INVALID_RESPONSE", "DashScope 响应缺少识别结果", 502) from exc
        text = self._extract_content_text(message.get("content"))
        if not text:
            raise AppError("ASR_EMPTY_RESULT", "语音识别完成但未返回文字", 502)
        language, emotion = self._extract_annotation(message.get("annotations"))
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return AsrTranscription(
            text=text,
            provider="dashscope",
            model=self._settings.asr_model,
            language=language,
            emotion=emotion,
            duration_seconds=self._to_float(usage.get("seconds")),
            upstream_request_id=self._string_or_none(body.get("request_id")),
        )

    def _parse_openai_transcription(self, body: dict[str, Any]) -> AsrTranscription:
        text = self._string_or_none(body.get("text"))
        if not text:
            raise AppError("ASR_EMPTY_RESULT", "语音识别完成但未返回文字", 502)
        return AsrTranscription(
            text=text,
            provider="openai_compatible",
            model=self._settings.asr_model,
            language=self._string_or_none(body.get("language")),
            duration_seconds=self._to_float(body.get("duration")),
            upstream_request_id=self._string_or_none(body.get("id")),
        )

    def _require_api_key(self) -> str:
        api_key = (self._settings.asr_api_key or "").strip()
        if not api_key:
            raise AppError(
                "ASR_API_KEY_MISSING",
                "服务端未配置 ASR_API_KEY",
                503,
            )
        return api_key

    @staticmethod
    def _dashscope_endpoint(base_url: str) -> str:
        base = base_url.rstrip("/")
        suffix = "/services/aigc/multimodal-generation/generation"
        if base.endswith(suffix):
            return base
        return f"{base}{suffix}"

    @staticmethod
    def _to_data_url(audio: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(audio).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _normalize_mime_type(value: str) -> str:
        mime = (value or "application/octet-stream").split(";", 1)[0].strip().lower()
        aliases = {
            "audio/x-wav": "audio/wav",
            "audio/mp3": "audio/mpeg",
            "audio/x-m4a": "audio/mp4",
        }
        return aliases.get(mime, mime)

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            pieces: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    value = item.get("text")
                    if isinstance(value, str) and value.strip():
                        pieces.append(value.strip())
                elif isinstance(item, str) and item.strip():
                    pieces.append(item.strip())
            return "".join(pieces).strip()
        return ""

    @staticmethod
    def _extract_annotation(annotations: Any) -> tuple[str | None, str | None]:
        if not isinstance(annotations, list):
            return None, None
        for item in annotations:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "audio_info"):
                continue
            return (
                AsrService._string_or_none(item.get("language")),
                AsrService._string_or_none(item.get("emotion")),
            )
        return None, None

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    @staticmethod
    def _safe_upstream_error(response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                for key in ("message", "error", "code"):
                    value = body.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:500]
                return str(body)[:500]
        except ValueError:
            pass
        return response.text.strip()[:500] or "unknown upstream error"
