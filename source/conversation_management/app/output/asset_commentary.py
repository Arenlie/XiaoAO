"""Incremental commentary after authoritative asset facts have already streamed."""
import re

_QUANTITY = re.compile(r"(?:\d[\d,.，\s~～至到-]*|[零〇一二两三四五六七八九十百千万亿]+)\s*(?:台|套)(?:设备|水泵|泵|风机|电机)?")
_BOUNDARY = re.compile(r"[。！？；\n]")


class AssetCommentaryStream:
    """Leave inventory counts/tables to backend facts, including during streaming.

    Buffers at most one short sentence, never the model's complete response.
    Industrial measurements (Hz, mm/s, °C, etc.) and citations remain supported.
    """
    def __init__(self):
        self.pending = ""
        self.discarding = False
        self.drop_leading_citations = False

    def _accept(self, sentence):
        if self.drop_leading_citations:
            sentence = re.sub(r"^\s*(?:\[\d+\]\s*)+", "", sentence)
        blocked = self.discarding or bool(_QUANTITY.search(sentence)) or "|" in sentence
        self.discarding = False
        self.drop_leading_citations = blocked
        return "" if blocked else sentence

    def feed(self, content, *, final=False):
        output = []
        self.pending += content
        while match := _BOUNDARY.search(self.pending):
            sentence, self.pending = self.pending[:match.end()], self.pending[match.end():]
            output.append(self._accept(sentence))
        if len(self.pending) > 2048:
            self.pending = ""
            self.discarding = True
        if final and self.pending:
            output.append(self._accept(self.pending))
            self.pending = ""
        return "".join(output)
