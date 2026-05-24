from __future__ import annotations

from typing import Iterable

from ..schema.attachment_schema import AttachmentSchema


class AttachmentCollector:
    def __init__(self) -> None:
        self._items: list[AttachmentSchema] = []
        self._seen: set[tuple[str, str]] = set()

    def add_many(self, attachments: Iterable[AttachmentSchema]) -> None:
        for attachment in attachments:
            dedupe_key = (attachment.file_id, attachment.file_url)
            if dedupe_key in self._seen:
                continue
            self._seen.add(dedupe_key)
            self._items.append(attachment)

    def list_items(self) -> list[AttachmentSchema]:
        return list(self._items)
