"""Incremental customer rendering and task-owned, two-channel final output."""
from __future__ import annotations

import asyncio
import re
import time
from uuid import uuid4

from app.output.customer import customer_text, knowledge_sources, redact_customer_secrets
from app.integrations.openai.chat_client import ModelStreamDelta
from app.performance import _finish_event

_HEADERS = ("参考文献", "参考资料", "知识库依据", "可继续询问", "建议追问")
_VALUE_KEYS = {"equip_no", "point_no", "device_code", "equip_name", "point_name", "space_name",
    "display_name", "param_code", "param_num", "raw_point_no", "equip_num", "point_num",
    "vibration_point_num", "temperature_point_num", "bias_param_num", "velocity_param_num", "temperature_param_num"}


def _verified_values(state):
    values = set()
    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in _VALUE_KEYS and isinstance(value, str) and value:
                    values.add(value)
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    for key in ("resolved_entity", "selected_entity", "observations"):
        walk(state.get(key))
    return sorted(values, key=lambda x: (-len(x), x))


class CustomerTextStream:
    """Hold only incomplete lexical tokens, citations and reserved header prefixes.

    Chinese prose is released as soon as available. Complete ASCII tokens allow field
    translation and credential redaction even when a model splits their spelling.
    """
    def __init__(self, state, *, references=True):
        self.pending = ""
        self.at_line_start = True
        self.suppressed = False
        self.references = references
        self.sources = knowledge_sources(state) if references else []
        self.used = []
        self.values = _verified_values(state)
        self._discard_token = False

    def _render(self, text):
        replacements = {}
        for n, value in enumerate(self.values):
            if value in text and re.search(r"[A-Za-z_]", value):
                placeholder = f"〔已核验业务值第{n}项〕"
                if placeholder not in text:
                    text = text.replace(value, placeholder)
                    replacements[placeholder] = value
        # Sentinels keep whitespace across stream boundaries; customer_text strips edges.
        text = customer_text("\ue100" + text + "\ue101")[1:-1]
        for key, value in replacements.items():
            text = text.replace(key, value)
        if self.references:
            def cite(match):
                number = int(match.group(1))
                if not 1 <= number <= len(self.sources):
                    return ""
                if number not in self.used:
                    self.used.append(number)
                return f"[{self.used.index(number) + 1}]"
            text = re.sub(r"\[(\d+)\]", cite, text)
        return text

    def feed(self, content, *, final=False):
        if self.suppressed:
            return ""
        self.pending += content
        output = []
        while self.pending:
            end = self.pending.find("\n")
            segment = self.pending if end < 0 else self.pending[:end + 1]
            if self.at_line_start and self.references:
                label = re.sub(r"^\s*#{0,4}\s*", "", segment).strip().strip("*")
                if any(label in {h, h+":", h+"："} for h in _HEADERS):
                    if end < 0 and not final:
                        break
                    self.suppressed = True
                    self.pending = ""
                    break
                if end < 0 and not final and (not label or any(h.startswith(label) for h in _HEADERS)):
                    break
                self.at_line_start = False
            cut = len(segment)
            if end < 0 and not final:
                # Whole ASCII lexical tokens, URLs and unfinished citation numbers stay private.
                match = re.search(r"[A-Za-z0-9_./:@?&=%+~\-]+$|\[\d*$", segment)
                if match:
                    cut = match.start()
                # Labels with spaces must survive arbitrary chunk boundaries too.
                for word in ("PHM Diagnosis MCP", "PHM Feature MCP", "PHM Data MCP", "PHM Asset MCP", "Bearer "):
                    for size in range(1, min(len(word), len(segment)) + 1):
                        if segment.endswith(word[:size]):
                            cut = min(cut, len(segment)-size)
                if cut == 0 and len(segment) > 2048:
                    # A very long unterminated encoding is not customer prose.
                    self.pending = ""
                    self._discard_token = True
                    output.append("（较长编码已省略）")
                    break
            if cut == 0:
                break
            text = segment[:cut]
            if self._discard_token:
                match = re.search(r"[^A-Za-z0-9_./:@?&=%+~\-]", text)
                text = text[match.start():] if match else ""
                self._discard_token = not bool(match)
            output.append(self._render(text))
            self.pending = self.pending[cut:]
            if text.endswith("\n"):
                self.at_line_start = True
        return "".join(output)

    def finish(self):
        return self.feed("", final=True)

    def citations(self):
        result = []
        for display_number, number in enumerate(self.used, 1):
            source = self.sources[number-1]
            pos = source.get("position")
            result.append({"number": display_number, "knowledge_base": source.get("knowledge_base"),
                "document_name": redact_customer_secrets(str(source.get("document_name") or "")).replace("\n", " "),
                "document_id": source.get("document_id"), "segment_id": source.get("segment_id"),
                "position": pos, "location": f"第 {pos} 知识片段" if isinstance(pos, int) and pos > 0 else "检索命中的知识片段",
                "excerpt": customer_text(str(source.get("content") or ""))[:240].replace("\n", " ")})
        return result

    def bibliography(self):
        rows = self.citations()
        if not rows:
            return ""
        return "\n\n---\n\n**知识库依据：**\n\n" + "\n\n".join(
            f"[{r['number']}] {r['knowledge_base']} · 《{r['document_name']}》 · {r['location']}：{r['excerpt']}" for r in rows)


class AnswerStreamStopped(Exception):
    pass


class AnswerStreamSession:
    def __init__(self, state, runtime, *, span_id=None, source="model"):
        self.state, self.runtime = state, runtime
        self.agent = runtime.agent_runtime
        self.generation_id = str(uuid4())
        self.base = {"generation_id": self.generation_id, "generation_span_id": span_id,
            "task_id": str(self.agent.task_id), "message_id": state.get("assistant_message_id")}
        self.meta = {**self.base, "citations": [], "content": "", "generation_source": source,
                     "reasoning_available": False}
        runtime.transient_tool_payloads["_answer_generation"] = self.meta
        self.started = time.perf_counter()
        self.source = source
        self.renderers = {"final": CustomerTextStream(state), "reasoning": CustomerTextStream(state, references=False)}
        self.reasoning_kind = None
        self.first = set()

    async def emit(self, event, data, *, status="STREAMING"):
        await self.agent.event_service.publish(self.agent.task_id, event, {**self.base, **data},
            graph_mode=str(self.state.get("execution_mode") or self.agent.execution_mode),
            actor_type="supervisor", actor_id="builtin.supervisor", status=status,
            span_id=self.state.get("root_span_id"))

    async def _delta(self, channel, content, kind=None):
        if not content:
            return
        if channel == "reasoning":
            if not getattr(self.agent.settings, "stream_reasoning_enabled", True):
                return
            self.meta["reasoning_available"] = True
            self.reasoning_kind = kind or self.reasoning_kind or "provider_output"
        else:
            self.meta["content"] += content
        if channel not in self.first:
            self.first.add(channel)
            self.meta[f"first_{channel}_ms"] = round((time.perf_counter()-self.started)*1000, 3)
        await self.emit("answer.reasoning.delta" if channel == "reasoning" else "answer.delta",
            {"channel": channel, "content": content, **({"reasoning_kind":self.reasoning_kind} if channel == "reasoning" else {})})

    async def run(self, events):
        await self.emit("answer.started", {"generation_source": self.source}, status="STARTED")
        queue = asyncio.Queue(maxsize=64)
        async def produce():
            try:
                async for event in events:
                    await queue.put(event)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError) and asyncio.current_task().cancelling():
                    raise
                await queue.put(exc)
            await queue.put(None)
        producer = asyncio.create_task(produce(), name="phm-final-model-stream")
        pending = []
        last_flush = time.perf_counter()
        interval = float(getattr(self.agent.settings, "answer_stream_flush_seconds", .08))
        async def flush():
            nonlocal last_flush
            for channel, content, kind in pending:
                await self._delta(channel, content, kind)
            pending.clear()
            last_flush = time.perf_counter()
        def append(channel, text, kind):
            if text:
                if pending and pending[-1][0] == channel and pending[-1][2] == kind:
                    pending[-1][1] += text
                else:
                    pending.append([channel, text, kind])
        status = "COMPLETED"
        try:
            while True:
                if callable(getattr(self.agent, "is_cancelled", None)) and await self.agent.is_cancelled():
                    raise AnswerStreamStopped()
                wait = max(.005, interval-(time.perf_counter()-last_flush)) if pending else .25
                try:
                    event = await asyncio.wait_for(queue.get(), wait)
                except TimeoutError:
                    await flush()
                    continue
                if isinstance(event, BaseException):
                    raise event
                if event is None:
                    break
                channel = event.channel
                if channel not in self.renderers:
                    continue
                text = self.renderers[channel].feed(event.content)
                append(channel, text, getattr(event, "reasoning_kind", None))
                if channel not in self.first or time.perf_counter()-last_flush >= interval:
                    await flush()
            for channel, renderer in self.renderers.items():
                append(channel, renderer.finish(), self.reasoning_kind)
            append("final", self.renderers["final"].bibliography(), None)
            await flush()
            self.meta["citations"] = self.renderers["final"].citations()
            return self.meta["content"]
        except (asyncio.CancelledError, AnswerStreamStopped):
            status = "CANCELLED"
            await flush()
            await _finish_event(self.emit("answer.cancelled", {"partial": bool(self.meta["content"]),
                "display_message": "分析已停止。"}, status=status))
            raise
        except Exception:
            status = "FAILED"
            await flush()
            await self.emit("answer.failed", {"partial": bool(self.meta["content"]),
                "error_code": "FINAL_MODEL_INTERRUPTED", "display_message": "回答生成中断，已显示内容可能不完整。"}, status=status)
            raise
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            available = self.meta["reasoning_available"]
            await _finish_event(self.emit("answer.reasoning.completed", {"channel":"reasoning",
                "available": available, "reasoning_kind": self.reasoning_kind,
                "finish_reason": "stream_finished" if available else "not_provided"},
                status=status if available or status != "COMPLETED" else "SKIPPED"))
            self.meta["status"] = status
