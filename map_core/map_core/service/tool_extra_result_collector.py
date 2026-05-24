from __future__ import annotations

from typing import Iterable

from ..schema.tool_extra_result_schema import ToolExtraResultSchema


class ToolExtraResultCollector:
    def __init__(self) -> None:
        self._items: list[ToolExtraResultSchema] = []
        self._seen_ids: set[str] = set()

    def add(self, item: ToolExtraResultSchema) -> None:
        if item.id in self._seen_ids:
            return
        self._seen_ids.add(item.id)
        self._items.append(item)

    def add_many(self, items: Iterable[ToolExtraResultSchema]) -> None:
        for item in items:
            self.add(item)

    def list_items(self) -> list[ToolExtraResultSchema]:
        return list(self._items)
