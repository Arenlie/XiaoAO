from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RealtimeAsrStatus:
    enabled: bool
    provider: str
    model: str


class DashScopeRealtimeAsrProxy:
    """Stream browser PCM16/16 kHz audio to Alibaba Cloud Model Studio.

    Browser protocol (stable for the built-in Chat UI):
      * browser -> backend: binary PCM16 little-endian mono 16 kHz chunks
      * browser -> backend: {"type": "finish"}
      * backend -> browser: ready / partial / final / finished / error

    Two DashScope upstream protocols are supported:
      * qwen_audio_streaming (default): qwen-audio-3.0-asr-flash-streaming,
        including context and instant hotword support.
      * qwen3_realtime: qwen3-asr-flash-realtime session protocol.

    API keys remain server-side and are never sent to the browser.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def status(self) -> RealtimeAsrStatus:
        return RealtimeAsrStatus(
            enabled=self._settings.asr_realtime_enabled,
            provider=self._settings.asr_realtime_provider,
            model=self._settings.asr_realtime_model,
        )

    async def run(self, browser: WebSocket, *, language: str | None) -> None:
        if not self._settings.asr_realtime_enabled:
            await self._send_error(browser, "ASR_REALTIME_DISABLED", "实时语音识别未启用")
            await browser.close(code=4403)
            return
        if self._settings.asr_realtime_provider != "dashscope":
            await self._send_error(
                browser,
                "ASR_REALTIME_PROVIDER_UNSUPPORTED",
                f"不支持的实时 ASR Provider: {self._settings.asr_realtime_provider}",
            )
            await browser.close(code=1011)
            return

        api_key = (self._settings.asr_realtime_api_key or self._settings.asr_api_key or "").strip()
        if not api_key:
            await self._send_error(browser, "ASR_API_KEY_MISSING", "服务端未配置实时 ASR API Key")
            await browser.close(code=1011)
            return

        try:
            if self._settings.asr_realtime_protocol == "qwen_audio_streaming":
                await self._run_qwen_audio_streaming(browser, api_key=api_key, language=language)
            else:
                await self._run_qwen3_realtime(browser, api_key=api_key, language=language)
        except InvalidStatus as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            message = "实时语音识别服务握手失败"
            if status_code in (401, 403):
                message = "实时语音识别鉴权失败，请检查 API Key、地域和 Workspace 配置"
            await self._safe_send_error(browser, "ASR_REALTIME_UPSTREAM_HANDSHAKE", message)
            log.warning("asr_realtime_upstream_handshake_failed", status_code=status_code)
        except (TimeoutError, asyncio.TimeoutError):
            await self._safe_send_error(browser, "ASR_REALTIME_UPSTREAM_TIMEOUT", "连接实时语音识别服务超时")
        except ConnectionClosed as exc:
            await self._safe_send_error(browser, "ASR_REALTIME_UPSTREAM_CLOSED", "实时语音识别上游连接已断开")
            log.warning("asr_realtime_upstream_closed", code=exc.code, reason=exc.reason)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("asr_realtime_proxy_failed", error=str(exc))
            await self._safe_send_error(browser, "ASR_REALTIME_INTERNAL_ERROR", "实时语音识别代理发生异常")
        finally:
            with suppress(Exception):
                await browser.close(code=1000)

    # ------------------------------------------------------------------
    # Qwen-Audio-3.0-ASR-Flash-Streaming protocol (preferred)
    # ------------------------------------------------------------------

    async def _run_qwen_audio_streaming(
        self,
        browser: WebSocket,
        *,
        api_key: str,
        language: str | None,
    ) -> None:
        task_id = str(uuid4())
        url = self._build_qwen_audio_url()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"{self._settings.app_name}/{self._settings.app_version}",
        }
        if self._settings.asr_realtime_workspace_id:
            headers["X-DashScope-WorkSpace"] = self._settings.asr_realtime_workspace_id

        async with websocket_connect(
            url,
            additional_headers=headers,
            open_timeout=self._settings.asr_realtime_connect_timeout_seconds,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=20 * 1024 * 1024,
        ) as upstream:
            await upstream.send(
                json.dumps(
                    self._qwen_audio_run_task(task_id=task_id, language=language),
                    ensure_ascii=False,
                )
            )
            await browser.send_json(
                {
                    "type": "upstream.connected",
                    "provider": "dashscope",
                    "model": self._settings.asr_realtime_model,
                }
            )
            await self._bridge_qwen_audio(browser, upstream, task_id=task_id)

    def _qwen_audio_run_task(self, *, task_id: str, language: str | None) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "format": "pcm",
            "sample_rate": 16000,
            "semantic_punctuation_enabled": False,
            "max_sentence_silence": self._settings.asr_realtime_max_sentence_silence_ms,
        }
        selected_language = (language or self._settings.asr_default_language or "").strip()
        if selected_language:
            parameters["language_hints"] = [selected_language]

        hotwords = self._realtime_hotwords()
        if hotwords:
            parameters["vocabulary"] = {
                word: self._settings.asr_realtime_hotword_weight for word in hotwords
            }

        input_payload: dict[str, Any] = {}
        context_text = (self._settings.asr_context or "").strip()
        if context_text:
            # Model Studio limits a single context turn to 400 characters.
            input_payload["context"] = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": context_text[:400],
                        }
                    ],
                }
            ]

        return {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self._settings.asr_realtime_model,
                "parameters": parameters,
                "input": input_payload,
            },
        }

    def _realtime_hotwords(self) -> list[str]:
        raw = (self._settings.asr_realtime_hotwords or self._settings.asr_context or "").strip()
        if not raw:
            return []
        pieces = re.split(r"[,，;；\n\r]+", raw)
        result: list[str] = []
        seen: set[str] = set()
        for piece in pieces:
            word = piece.strip()
            if not word or word in seen:
                continue
            # Avoid accidentally treating a long prose context paragraph as one hotword.
            if len(word) > 64:
                continue
            seen.add(word)
            result.append(word)
            if len(result) >= 200:
                break
        return result

    async def _bridge_qwen_audio(
        self,
        browser: WebSocket,
        upstream: ClientConnection,
        *,
        task_id: str,
    ) -> None:
        browser_task = asyncio.create_task(self._browser_to_qwen_audio(browser, upstream))
        upstream_task = asyncio.create_task(self._qwen_audio_to_browser(browser, upstream))
        try:
            done, _ = await asyncio.wait(
                {browser_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if upstream_task in done:
                browser_task.cancel()
                with suppress(asyncio.CancelledError):
                    await browser_task
                return

            reason = browser_task.result()
            if reason in {"finish", "disconnect"}:
                with suppress(Exception):
                    await upstream.send(
                        json.dumps(
                            {
                                "header": {
                                    "action": "finish-task",
                                    "task_id": task_id,
                                    "streaming": "duplex",
                                },
                                "payload": {"input": {}},
                            }
                        )
                    )
                try:
                    await asyncio.wait_for(
                        upstream_task,
                        timeout=self._settings.asr_realtime_finish_timeout_seconds,
                    )
                except TimeoutError:
                    upstream_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await upstream_task
        finally:
            for task in (browser_task, upstream_task):
                if not task.done():
                    task.cancel()
            for task in (browser_task, upstream_task):
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _browser_to_qwen_audio(
        self,
        browser: WebSocket,
        upstream: ClientConnection,
    ) -> str:
        while True:
            try:
                message = await browser.receive()
            except WebSocketDisconnect:
                return "disconnect"
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                return "disconnect"
            audio = message.get("bytes")
            if audio:
                # Qwen-Audio streaming protocol accepts raw binary audio frames.
                await upstream.send(audio)
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            control = str(payload.get("type") or "")
            if control == "finish":
                return "finish"
            if control == "ping":
                await browser.send_json({"type": "pong"})

    async def _qwen_audio_to_browser(
        self,
        browser: WebSocket,
        upstream: ClientConnection,
    ) -> None:
        async for raw in upstream:
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            header = event.get("header") or {}
            event_type = str(header.get("event") or "")
            if event_type == "task-started":
                await browser.send_json(
                    {
                        "type": "ready",
                        "sample_rate": 16000,
                        "audio_format": "pcm16le",
                        "silence_duration_ms": self._settings.asr_realtime_max_sentence_silence_ms,
                    }
                )
            elif event_type == "result-generated":
                sentence = (((event.get("payload") or {}).get("output") or {}).get("sentence") or {})
                text = str(sentence.get("text") or "")
                if not text and not sentence.get("sentence_begin"):
                    continue
                if bool(sentence.get("sentence_end")):
                    await browser.send_json(
                        {
                            "type": "final",
                            "text": text,
                            "sentence_id": sentence.get("sentence_id"),
                            "begin_time": sentence.get("begin_time"),
                            "end_time": sentence.get("end_time"),
                        }
                    )
                else:
                    await browser.send_json(
                        {
                            "type": "partial",
                            "text": text,
                            "confirmed": "",
                            "stash": text,
                            "sentence_id": sentence.get("sentence_id"),
                        }
                    )
            elif event_type == "task-finished":
                await browser.send_json({"type": "finished"})
                with suppress(RuntimeError):
                    await browser.close(code=1000)
                return
            elif event_type == "task-failed":
                await browser.send_json(
                    {
                        "type": "error",
                        "code": str(header.get("error_code") or "ASR_TRANSCRIPTION_FAILED"),
                        "message": str(header.get("error_message") or "实时语音识别失败"),
                    }
                )
                return

    def _build_qwen_audio_url(self) -> str:
        return self._settings.asr_realtime_base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Qwen3-ASR-Flash-Realtime session protocol (compatibility option)
    # ------------------------------------------------------------------

    async def _run_qwen3_realtime(
        self,
        browser: WebSocket,
        *,
        api_key: str,
        language: str | None,
    ) -> None:
        url = self._build_qwen3_url()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "realtime=v1",
            "User-Agent": f"{self._settings.app_name}/{self._settings.app_version}",
        }
        if self._settings.asr_realtime_workspace_id:
            headers["X-DashScope-WorkSpace"] = self._settings.asr_realtime_workspace_id

        async with websocket_connect(
            url,
            additional_headers=headers,
            open_timeout=self._settings.asr_realtime_connect_timeout_seconds,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=20 * 1024 * 1024,
        ) as upstream:
            await self._send_qwen3_session_update(upstream, language=language)
            await browser.send_json(
                {
                    "type": "upstream.connected",
                    "provider": "dashscope",
                    "model": self._settings.asr_realtime_model,
                }
            )
            await self._bridge_qwen3(browser, upstream)

    def _build_qwen3_url(self) -> str:
        base = self._settings.asr_realtime_base_url.rstrip("/")
        separator = "&" if "?" in base else "?"
        if "model=" in base:
            return base
        return f"{base}{separator}model={self._settings.asr_realtime_model}"

    async def _send_qwen3_session_update(
        self,
        upstream: ClientConnection,
        *,
        language: str | None,
    ) -> None:
        session: dict[str, Any] = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": 16000,
            "turn_detection": {
                "type": "server_vad",
                "threshold": self._settings.asr_realtime_vad_threshold,
                "silence_duration_ms": self._settings.asr_realtime_silence_duration_ms,
            },
        }
        selected_language = (language or self._settings.asr_default_language or "").strip()
        if selected_language:
            session["input_audio_transcription"] = {"language": selected_language}
        await upstream.send(
            json.dumps(
                {"event_id": self._event_id(), "type": "session.update", "session": session},
                ensure_ascii=False,
            )
        )

    async def _bridge_qwen3(self, browser: WebSocket, upstream: ClientConnection) -> None:
        frontend_task = asyncio.create_task(self._browser_to_qwen3(browser, upstream))
        upstream_task = asyncio.create_task(self._qwen3_to_browser(browser, upstream))
        try:
            done, _ = await asyncio.wait(
                {frontend_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if upstream_task in done:
                frontend_task.cancel()
                with suppress(asyncio.CancelledError):
                    await frontend_task
                return
            finish_reason = frontend_task.result()
            if finish_reason in {"finish", "disconnect"}:
                with suppress(Exception):
                    await upstream.send(
                        json.dumps({"event_id": self._event_id(), "type": "session.finish"})
                    )
                try:
                    await asyncio.wait_for(
                        upstream_task,
                        timeout=self._settings.asr_realtime_finish_timeout_seconds,
                    )
                except TimeoutError:
                    upstream_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await upstream_task
        finally:
            for task in (frontend_task, upstream_task):
                if not task.done():
                    task.cancel()
            for task in (frontend_task, upstream_task):
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _browser_to_qwen3(self, browser: WebSocket, upstream: ClientConnection) -> str:
        while True:
            try:
                message = await browser.receive()
            except WebSocketDisconnect:
                return "disconnect"
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                return "disconnect"
            audio = message.get("bytes")
            if audio:
                encoded = base64.b64encode(audio).decode("ascii")
                await upstream.send(
                    json.dumps(
                        {
                            "event_id": self._event_id(),
                            "type": "input_audio_buffer.append",
                            "audio": encoded,
                        }
                    )
                )
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            control = str(payload.get("type") or "")
            if control == "finish":
                return "finish"
            if control == "ping":
                await browser.send_json({"type": "pong"})

    async def _qwen3_to_browser(self, browser: WebSocket, upstream: ClientConnection) -> None:
        async for raw in upstream:
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "session.updated":
                await browser.send_json(
                    {
                        "type": "ready",
                        "sample_rate": 16000,
                        "audio_format": "pcm16le",
                        "silence_duration_ms": self._settings.asr_realtime_silence_duration_ms,
                    }
                )
            elif event_type == "input_audio_buffer.speech_started":
                await browser.send_json({"type": "speech.started"})
            elif event_type == "input_audio_buffer.speech_stopped":
                await browser.send_json({"type": "speech.stopped"})
            elif event_type == "conversation.item.input_audio_transcription.text":
                confirmed = str(event.get("text") or "")
                stash = str(event.get("stash") or "")
                await browser.send_json(
                    {
                        "type": "partial",
                        "text": f"{confirmed}{stash}",
                        "confirmed": confirmed,
                        "stash": stash,
                        "language": event.get("language"),
                        "emotion": event.get("emotion"),
                        "item_id": event.get("item_id"),
                    }
                )
            elif event_type == "conversation.item.input_audio_transcription.completed":
                await browser.send_json(
                    {
                        "type": "final",
                        "text": str(event.get("transcript") or ""),
                        "language": event.get("language"),
                        "emotion": event.get("emotion"),
                        "item_id": event.get("item_id"),
                    }
                )
            elif event_type == "conversation.item.input_audio_transcription.failed":
                await browser.send_json(
                    {
                        "type": "error",
                        "code": "ASR_TRANSCRIPTION_FAILED",
                        "message": self._extract_upstream_error(event) or "实时语音识别失败",
                    }
                )
            elif event_type == "error":
                await browser.send_json(
                    {
                        "type": "error",
                        "code": str((event.get("error") or {}).get("code") or "ASR_UPSTREAM_ERROR"),
                        "message": self._extract_upstream_error(event) or "实时语音识别服务返回错误",
                    }
                )
            elif event_type == "session.finished":
                await browser.send_json({"type": "finished"})
                with suppress(RuntimeError):
                    await browser.close(code=1000)
                return

    @staticmethod
    def _extract_upstream_error(event: dict[str, Any]) -> str | None:
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = event.get("message")
        return message.strip() if isinstance(message, str) and message.strip() else None

    @staticmethod
    def _event_id() -> str:
        return f"event_{uuid4().hex}"

    @staticmethod
    async def _send_error(browser: WebSocket, code: str, message: str) -> None:
        await browser.send_json({"type": "error", "code": code, "message": message})

    @staticmethod
    async def _safe_send_error(browser: WebSocket, code: str, message: str) -> None:
        with suppress(Exception):
            await browser.send_json({"type": "error", "code": code, "message": message})
