from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.attachments.contracts import AttachmentDescriptor
from app.content.contracts import ContentKind, ContentUnderstandingResult, ExtractionStatus

_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs",
    ".sql", ".sh", ".ps1", ".html", ".css", ".xml", ".toml", ".ini",
}


def decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


class TextContentParser:
    name = "text.v1"
    supported_kinds = {"text"}

    async def parse(
        self,
        *,
        descriptor: AttachmentDescriptor,
        data: bytes,
        max_characters: int,
    ) -> ContentUnderstandingResult:
        text, encoding = decode_text(data)
        extension = Path(descriptor.filename).suffix.lower()
        kind = ContentKind.CODE if extension in _CODE_EXTENSIONS else ContentKind.TEXT
        warnings: list[str] = []
        structured: dict = {}
        if extension == ".json":
            try:
                value = json.loads(text)
                structured = {"json_root_type": type(value).__name__}
            except json.JSONDecodeError as exc:
                warnings.append(f"JSON 结构校验失败：{exc.msg}")
        preview = text[:max_characters]
        if len(text) > max_characters:
            warnings.append("文本较长，统一理解层仅保留前部预览；后续应使用文件检索或模型原生文件输入。")
        return ContentUnderstandingResult(
            content_id=str(descriptor.attachment_id),
            attachment_id=descriptor.attachment_id,
            kind=kind,
            extraction_status=ExtractionStatus.COMPLETED,
            summary=f"文本文件 {descriptor.filename}，约 {len(text)} 个字符，编码 {encoding}。",
            extracted_text=preview,
            code_files=[{"path": descriptor.filename, "characters": len(text)}] if kind == ContentKind.CODE else [],
            capabilities_required=["code_analysis"] if kind == ContentKind.CODE else ["text_reasoning"],
            warnings=warnings,
            parser_name=self.name,
            metadata=structured,
        )


class CsvContentParser:
    name = "csv.v1"
    supported_kinds = {"spreadsheet"}

    async def parse(
        self,
        *,
        descriptor: AttachmentDescriptor,
        data: bytes,
        max_characters: int,
    ) -> ContentUnderstandingResult:
        text, encoding = decode_text(data)
        sample = text[: min(len(text), 65536)]
        delimiter = ","
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            pass
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows: list[list[str]] = []
        total_rows = 0
        max_columns = 0
        for row in reader:
            total_rows += 1
            max_columns = max(max_columns, len(row))
            if len(rows) < 30:
                rows.append(row[:50])
        headers = rows[0] if rows else []
        preview_lines = [delimiter.join(row) for row in rows]
        return ContentUnderstandingResult(
            content_id=str(descriptor.attachment_id),
            attachment_id=descriptor.attachment_id,
            kind=ContentKind.SPREADSHEET,
            extraction_status=ExtractionStatus.COMPLETED,
            summary=(
                f"表格文件 {descriptor.filename}，约 {total_rows} 行、最多 {max_columns} 列，"
                f"编码 {encoding}。"
            ),
            extracted_text="\n".join(preview_lines)[:max_characters],
            sheets=[{
                "name": "CSV",
                "rows": total_rows,
                "columns": max_columns,
                "headers": headers[:50],
                "preview_rows": rows[:10],
            }],
            capabilities_required=["spreadsheet_inspection"],
            parser_name=self.name,
        )
