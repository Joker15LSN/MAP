from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from pydantic import ValidationError

from ...schema.attachment_schema import AttachmentSchema
from .base import AgentRequest
from .tool_call_agent import Tool

TOOL_NAME = "attachment_file_read_tool"
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "latin-1")
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_CHARS_PER_FILE = 12000
_TEXT_EXTENSIONS = {
    "txt",
    "text",
    "md",
    "markdown",
    "json",
    "jsonl",
    "yaml",
    "yml",
    "xml",
    "csv",
    "log",
    "html",
    "htm",
    "py",
    "js",
    "ts",
    "sql",
    "ini",
    "conf",
}
_BINARY_EXTENSIONS = {
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "zip",
    "rar",
    "7z",
    "tar",
    "gz",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
    "mp3",
    "mp4",
}


def _extract_attachments(args: dict[str, Any], request: Any) -> list[Any]:
    # Priority: explicit args > request.attachments > request.extra.attachments
    from_args = args.get("attachments")
    if isinstance(from_args, list):
        return from_args

    candidate = getattr(request, "attachments", None)
    if isinstance(candidate, list):
        return candidate

    request_extra = getattr(request, "extra", None)
    if isinstance(request_extra, dict):
        extra_attachments = request_extra.get("attachments")
        if isinstance(extra_attachments, list):
            return extra_attachments

    return []


def _normalize_attachment(item: Any) -> AttachmentSchema:
    if isinstance(item, AttachmentSchema):
        return item
    return AttachmentSchema.model_validate(item)


def _is_probably_text(raw: bytes) -> bool:
    if not raw:
        return True
    sample = raw[:4096]
    if b"\x00" in sample:
        return False
    non_text = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return (non_text / len(sample)) < 0.3


def _guess_extension(file_type: str, file_name: str, file_url: str) -> str:
    for candidate in (file_type, Path(file_name).suffix.lstrip(".")):
        ext = str(candidate or "").strip().lower().lstrip(".")
        if ext:
            return ext

    parsed_path = urlparse(file_url).path
    return Path(parsed_path).suffix.lstrip(".").lower()


def _is_text_by_extension(file_type: str, file_name: str, file_url: str) -> bool | None:
    ext = _guess_extension(file_type, file_name, file_url)
    if not ext:
        return None
    if ext in _TEXT_EXTENSIONS:
        return True
    if ext in _BINARY_EXTENSIONS:
        return False
    return None


def _decode_text(
    raw: bytes, file_type: str, file_name: str, file_url: str
) -> str | None:
    ext_judgement = _is_text_by_extension(file_type, file_name, file_url)
    if ext_judgement is False:
        return None
    if ext_judgement is None and not _is_probably_text(raw):
        return None

    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_from_local_path(file_url: str) -> bytes:
    raw_path = file_url
    if file_url.startswith("file://"):
        parsed = urlparse(file_url)
        raw_path = unquote(parsed.path)
    return Path(raw_path).expanduser().read_bytes()


def _read_from_url(file_url: str, timeout_seconds: float) -> bytes:
    response = httpx.get(file_url, timeout=timeout_seconds, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _resolve_file_bytes(file_url: str, timeout_seconds: float) -> bytes:
    lowered = file_url.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return _read_from_url(file_url, timeout_seconds)
    return _read_from_local_path(file_url)


def _format_file_header(item: AttachmentSchema) -> str:
    file_id = item.file_id
    file_name = item.file_name
    file_type = item.file_type
    file_url = item.file_url
    return (
        f"file_id={file_id or '-'} | "
        f"file_name={file_name or '-'} | "
        f"file_type={file_type or '-'} | "
        f"file_url={file_url or '-'}"
    )


def create_attachment_file_read_tool() -> Tool:
    async def _handler(
        args: dict[str, Any], request: Any, _parid: str
    ) -> dict[str, Any]:
        timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
        max_chars_per_file = _DEFAULT_MAX_CHARS_PER_FILE

        attachments = _extract_attachments(args, request)
        if not attachments:
            return {
                "success": False,
                "error": (
                    "No attachment list found. Please provide a list in "
                    "`args.attachments`, `request.attachments`, or "
                    "`request.extra.attachments`."
                ),
            }

        raw_ids = args.get("file_ids")
        file_ids = (
            {str(file_id) for file_id in raw_ids} if isinstance(raw_ids, list) else None
        )

        def _read_sync() -> dict[str, Any]:
            file_results: list[dict[str, Any]] = []
            combined_parts: list[str] = []

            for raw_item in attachments:
                try:
                    item = _normalize_attachment(raw_item)
                except ValidationError as exc:
                    file_results.append(
                        {
                            "success": False,
                            "file": raw_item,
                            "error": (
                                "Invalid attachment item. Required non-empty fields: "
                                "file_id, file_name, file_type, file_url. "
                                f"details={exc.errors()}"
                            ),
                        }
                    )
                    continue

                file_id = item.file_id
                if file_ids is not None and file_id not in file_ids:
                    continue

                try:
                    file_bytes = _resolve_file_bytes(
                        item.file_url.strip(), timeout_seconds
                    )
                    text = _decode_text(
                        file_bytes,
                        file_type=item.file_type,
                        file_name=item.file_name,
                        file_url=item.file_url,
                    )
                    if text is None:
                        raise ValueError(
                            "Binary/unsupported file content. Please provide text-compatible files."
                        )

                    truncated = len(text) > max_chars_per_file
                    text_for_llm = text[:max_chars_per_file]
                    header = _format_file_header(item)
                    combined_parts.append(
                        f"[Attachment]\n{header}\n[Content]\n{text_for_llm}"
                    )
                    file_results.append(
                        {
                            "success": True,
                            "file": item.model_dump(),
                            "content": text_for_llm,
                            "truncated": truncated,
                            "original_chars": len(text),
                        }
                    )
                except Exception as exc:
                    file_results.append(
                        {
                            "success": False,
                            "file": item.model_dump(),
                            "error": str(exc),
                        }
                    )

            success_files = sum(1 for item in file_results if item.get("success"))
            return {
                "success": success_files > 0,
                "combined_text": "\n\n".join(combined_parts),
                "files": file_results,
                "stats": {
                    "total_files": len(file_results),
                    "success_files": success_files,
                    "failed_files": len(file_results) - success_files,
                },
            }

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_read_sync), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {timeout_seconds} seconds.",
            }

    return Tool(
        name=TOOL_NAME,
        description=(
            "Read files from request attachment list and return text suitable for LLM consumption."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file_id whitelist. If omitted, reads all attachments.",
                },
            },
            "additionalProperties": False,
        },
        handler=_handler,
    )


if __name__ == "__main__":

    async def _demo() -> None:
        # Set MAP_TEST_FILE_URL to test MinIO/http URL quickly.
        file_url = os.getenv(
            "MAP_TEST_FILE_URL",
            "http://10.16.11.40:9000/map_core/generated/6ff5285e6aa74b74b0e8f59c9fd67793/2026/03/05/e2cf65109ec1451ab2d622d2163f2234_hello.txt",
        ).strip()
        if not file_url:
            print(
                "Please set env MAP_TEST_FILE_URL, for example:\n"
                "MAP_TEST_FILE_URL='http://10.16.11.40:9000/map_core/generated/.../hello.txt' "
                "uv run python -m map_core.service.agent.file_read_tool"
            )
            return

        tool = create_attachment_file_read_tool()
        request = AgentRequest(query="demo read attachment", staff_code="demo")
        request = request.model_copy(
            update={
                "attachments": [
                    AttachmentSchema(
                        file_id="demo-file-1",
                        file_name=Path(file_url).name or "demo.txt",
                        file_type=Path(file_url).suffix.lstrip(".") or "txt",
                        file_url=file_url,
                    ).model_dump()
                ]
            }
        )

        args = {
            "file_ids": ["demo-file-1"],
        }
        result = await tool.run(args, request, "-")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(_demo())
