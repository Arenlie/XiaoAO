from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Channel = Literal["final", "reasoning"]


@dataclass(slots=True, frozen=True)
class TextSegment:
    channel: Channel
    content: str


_TAG_RE = re.compile(r"<\s*(/?)\s*(think|analysis|reasoning)\s*>", re.IGNORECASE)
_VALID_COMPACT_TAGS = (
    "<think>",
    "</think>",
    "<analysis>",
    "</analysis>",
    "<reasoning>",
    "</reasoning>",
)


def _coalesce(segments: list[TextSegment]) -> list[TextSegment]:
    result: list[TextSegment] = []
    for segment in segments:
        if not segment.content:
            continue
        if result and result[-1].channel == segment.channel:
            previous = result[-1]
            result[-1] = TextSegment(previous.channel, previous.content + segment.content)
        else:
            result.append(segment)
    return result


class ThinkTagStreamSplitter:
    """Split model output into final-answer and execution/reasoning channels.

    Besides removing ``<think>...</think>`` tags, this parser handles common
    ReAct output shaped as::

        <think>reason 1</think>tool narration
        <think>reason 2</think>final answer

    In the default buffered mode, plain text between two think blocks is treated
    as execution narration and only text after the *last* closing tag becomes the
    final answer. Public SSE output uses ``eager_final_after_reasoning=True``:
    reasoning is suppressed as soon as an opening tag is detected and text is
    released immediately after the closing tag so the user sees a real stream.
    """

    def __init__(self, *, eager_final_after_reasoning: bool = False) -> None:
        self._buffer = ""
        self._inside_reasoning = False
        self._saw_reasoning_tag = False
        self._pending_final = ""
        # Public answer streaming uses eager mode: suppress everything inside the
        # reasoning tag, then release text immediately after the closing tag.
        # The legacy buffered mode is retained for full-text cleanup because it can
        # reclassify ReAct narration that appears between multiple reasoning blocks.
        self._eager_final_after_reasoning = eager_final_after_reasoning

    @property
    def channel(self) -> Channel:
        return "reasoning" if self._inside_reasoning else "final"

    def feed(self, chunk: str) -> list[TextSegment]:
        if not chunk:
            return []
        self._buffer += str(chunk)
        output: list[TextSegment] = []

        while self._buffer:
            match = _TAG_RE.search(self._buffer)
            if match is not None:
                before = self._buffer[: match.start()]
                self._accept_text(before, output)

                is_closing = bool(match.group(1))
                if not is_closing:
                    # Text after a previous </think> was only a candidate final
                    # answer. A new <think> proves it was intermediate narration.
                    if self._pending_final:
                        output.append(TextSegment("reasoning", self._pending_final))
                        self._pending_final = ""
                    self._saw_reasoning_tag = True
                    self._inside_reasoning = True
                else:
                    self._saw_reasoning_tag = True
                    self._inside_reasoning = False

                self._buffer = self._buffer[match.end() :]
                continue

            emit_upto = self._safe_emit_upto(self._buffer)
            if emit_upto > 0:
                self._accept_text(self._buffer[:emit_upto], output)
                self._buffer = self._buffer[emit_upto:]
            break

        return _coalesce(output)

    def flush(self) -> list[TextSegment]:
        output: list[TextSegment] = []
        if self._buffer:
            residual = self._buffer
            self._buffer = ""
            compact = re.sub(r"\s+", "", residual).lower()
            # A truncated control tag is protocol syntax rather than content.
            if not any(tag.startswith(compact) for tag in _VALID_COMPACT_TAGS):
                self._accept_text(residual, output)

        if self._pending_final:
            output.append(TextSegment("final", self._pending_final))
            self._pending_final = ""
        return _coalesce(output)

    def _accept_text(self, value: str, output: list[TextSegment]) -> None:
        if not value:
            return
        if self._inside_reasoning:
            output.append(TextSegment("reasoning", value))
        elif self._saw_reasoning_tag and not self._eager_final_after_reasoning:
            self._pending_final += value
        else:
            output.append(TextSegment("final", value))

    @staticmethod
    def _safe_emit_upto(value: str) -> int:
        last_lt = value.rfind("<")
        if last_lt < 0:
            return len(value)
        suffix = value[last_lt:]
        compact = re.sub(r"\s+", "", suffix).lower()
        if len(suffix) <= 64 and any(
            tag.startswith(compact) for tag in _VALID_COMPACT_TAGS
        ):
            return last_lt
        return len(value)


def split_think_text(value: str) -> tuple[str, str]:
    """Return ``(final_answer, reasoning_and_execution_narration)``."""

    splitter = ThinkTagStreamSplitter()
    segments = splitter.feed(str(value or "")) + splitter.flush()
    final = "".join(item.content for item in segments if item.channel == "final")
    reasoning = "".join(
        item.content for item in segments if item.channel == "reasoning"
    )
    return final, reasoning


def strip_think_blocks(value: str) -> str:
    return split_think_text(value)[0]
