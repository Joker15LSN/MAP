from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime

from pymongo.database import Database

from app.core.config import Settings
from app.core.database import MongoCollections
from app.services.agents_tools import (
    AgentToolRepository,
    build_agents_rows,
    build_tools_payload,
)
from app.services.container_mapping import (
    MAIN_FLOW_CONTAINERS,
    assert_container_supported,
    enforce_container_tool,
    mapped_tool_for_container,
)
from app.services.filters import (
    FilterOptions,
    build_agent_match,
    build_request_match,
    build_tool_match,
)
from app.services.llm_calls import (
    EMPTY_LLM_CALLS_PAYLOAD,
    LlmRepository,
    build_llm_calls_payload,
)
from app.services.loki_query_service import LokiQueryService
from app.services.math_utils import to_float
from app.services.requests import (
    RequestRepository,
    build_overview_payload,
    build_request_items,
    build_request_payload,
    build_trends_rows,
    build_users_rows,
)
from app.services.serializers import (
    build_agent_timeline,
    extract_request_ids_from_rows,
    is_retryable_loki_error,
    json_default,
    merge_tool_call_rows,
    to_ns,
)

EXCLUDED_AGENT_CODES_FOR_DURATION = {"globaldomainorchestrator"}
LOKI_QUERY_LIMIT = 1000
MIN_SPLIT_WINDOW_SECONDS = 60
CONTAINER_QUERY_CHUNK_SECONDS = 6 * 60 * 60
CONTAINER_REQUEST_VERIFY_WINDOW_SECONDS = 120
MAX_CONTAINER_FALLBACK_CANDIDATES = 500
CONTAINER_TIMEOUT_PRONE_MAIN_FLOW = {"map_core-test", "map_core-preprod"}
CONTAINER_FALLBACK_FIRST_RANGE_HOURS = 24

OVERVIEW_PROJECTION = {
    "_id": 0,
    "request_id": 1,
    "status": 1,
    "duration_s": 1,
    "token_usage_total": 1,
    "scene_result": 1,
}

USERS_PROJECTION = {
    "_id": 0,
    "request_id": 1,
    "staff_code": 1,
    "status": 1,
    "duration_s": 1,
    "token_usage_total": 1,
}

LIST_PROJECTION = {
    "_id": 0,
    "request_id": 1,
    "session_id": 1,
    "staff_code": 1,
    "status": 1,
    "duration_s": 1,
    "start_ts": 1,
    "end_ts": 1,
    "query": 1,
    "agents_called": 1,
    "token_usage_total": 1,
}

DETAIL_PROJECTION = {
    "_id": 0,
    "request_id": 1,
    "state_id": 1,
    "session_id": 1,
    "staff_code": 1,
    "query": 1,
    "status": 1,
    "error": 1,
    "start_ts": 1,
    "end_ts": 1,
    "duration_s": 1,
    "agents_called": 1,
    "scene_result": 1,
    "token_usage_total": 1,
}


class AnalyticsService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        collections: MongoCollections | None = None,
        loki_query_service: LokiQueryService | None = None,
        trusted_container_filters: Iterable[str] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.collections = collections or MongoCollections()
        self.loki_query_service = loki_query_service
        self.trusted_container_filters = set(trusted_container_filters or [])

        self.request_collection = self.database[self.collections.request_records]
        self.agent_collection = self.database[self.collections.agent_executions]
        self.tool_collection = self.database[self.collections.tool_call_records]
        self.llm_collection = self.database[self.collections.llm_call_records]

        self.request_repo = RequestRepository(self.request_collection)
        self.agent_tool_repo = AgentToolRepository(self.agent_collection, self.tool_collection)
        self.llm_repo = LlmRepository(self.llm_collection)

    def _request_id_set_from_agents(self, filters: FilterOptions) -> set:
        return self.agent_tool_repo.request_ids_from_agents(filters)

    def _request_id_set_from_tools(self, filters: FilterOptions) -> set:
        return self.agent_tool_repo.request_ids_from_tools(filters)

    @staticmethod
    def _assert_container(container: str) -> str:
        return assert_container_supported(container)

    def _should_filter_container_with_loki(self, container: str) -> bool:
        if self.loki_query_service is None or not self.loki_query_service.is_enabled():
            return False
        return container not in self.trusted_container_filters

    def _collect_container_request_ids_split(
        self,
        container: str,
        start_ns: int,
        end_ns: int,
    ) -> set[str]:
        midpoint = start_ns + ((end_ns - start_ns) // 2)
        left_ids = self._collect_container_request_ids_recursive(container, start_ns, midpoint)
        right_ids = self._collect_container_request_ids_recursive(container, midpoint + 1, end_ns)
        return left_ids.union(right_ids)

    def _collect_container_request_ids_chunked(
        self,
        container: str,
        start_ns: int,
        end_ns: int,
    ) -> set[str]:
        if start_ns > end_ns:
            return set()

        chunk_ns = CONTAINER_QUERY_CHUNK_SECONDS * 1_000_000_000
        collected_ids: set[str] = set()
        cursor = start_ns
        while cursor <= end_ns:
            chunk_end = min(cursor + chunk_ns - 1, end_ns)
            collected_ids.update(
                self._collect_container_request_ids_recursive(
                    container=container,
                    start_ns=cursor,
                    end_ns=chunk_end,
                )
            )
            cursor = chunk_end + 1
        return collected_ids

    def _request_id_set_from_container_fallback(
        self, filters: FilterOptions, container: str
    ) -> set[str]:
        match = build_request_match(filters)
        candidate_count = self.request_collection.count_documents(match)
        cursor = self.request_collection.find(
            match,
            {
                "request_id": 1,
                "start_ts": 1,
                "end_ts": 1,
            },
        ).sort("start_ts", -1)
        if candidate_count > MAX_CONTAINER_FALLBACK_CANDIDATES:
            cursor = cursor.limit(MAX_CONTAINER_FALLBACK_CANDIDATES)
        candidates = [doc for doc in cursor if doc.get("request_id")]

        verify_window_ns = CONTAINER_REQUEST_VERIFY_WINDOW_SECONDS * 1_000_000_000
        matched: set[str] = set()
        for doc in candidates:
            request_id = str(doc.get("request_id"))
            raw_start = (
                doc.get("start_ts")
                if isinstance(doc.get("start_ts"), datetime)
                else filters.start_ts
            )
            raw_end = doc.get("end_ts") if isinstance(doc.get("end_ts"), datetime) else raw_start
            start_ns = max(0, to_ns(raw_start) - verify_window_ns)
            end_ns = max(start_ns, to_ns(raw_end) + verify_window_ns)
            selector = f'{{container="{container}"}}'
            query = f'{selector} |= "{request_id}"'
            try:
                rows = self.loki_query_service.query_range(
                    query=query,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    limit=1,
                    direction="forward",
                )
            except RuntimeError:
                continue
            if rows:
                matched.add(request_id)

        return matched

    def _collect_container_request_ids_recursive(
        self,
        container: str,
        start_ns: int,
        end_ns: int,
    ) -> set[str]:
        if start_ns > end_ns:
            return set()

        selector = f'{{container="{container}"}}'
        query = f'{selector} |~ "(?i)(rid=|rid:|request_id=|request_id:|req_id=|req_id:)"'
        try:
            rows = self.loki_query_service.query_range(
                query=query,
                start_ns=start_ns,
                end_ns=end_ns,
                limit=LOKI_QUERY_LIMIT,
                direction="forward",
            )
        except RuntimeError as exc:
            window_ns = end_ns - start_ns
            min_split_window_ns = MIN_SPLIT_WINDOW_SECONDS * 1_000_000_000
            if window_ns > min_split_window_ns and is_retryable_loki_error(exc):
                return self._collect_container_request_ids_split(
                    container=container,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
            raise
        request_ids = extract_request_ids_from_rows(rows)

        window_ns = end_ns - start_ns
        min_split_window_ns = MIN_SPLIT_WINDOW_SECONDS * 1_000_000_000
        if len(rows) < LOKI_QUERY_LIMIT or window_ns <= min_split_window_ns:
            return request_ids

        split_ids = self._collect_container_request_ids_split(container, start_ns, end_ns)
        return request_ids.union(split_ids)

    def _request_id_set_from_container(self, filters: FilterOptions) -> set[str]:
        if not filters.container:
            return set()

        normalized_container = self._assert_container(filters.container)
        if self.loki_query_service is None or not self.loki_query_service.is_enabled():
            raise RuntimeError("container filtering requires Grafana/Loki configuration")

        start_ns = to_ns(filters.start_ts)
        end_ns = to_ns(filters.end_ts)
        range_ns = max(0, end_ns - start_ns)
        fallback_first_threshold_ns = CONTAINER_FALLBACK_FIRST_RANGE_HOURS * 60 * 60 * 1_000_000_000
        if (
            normalized_container in MAIN_FLOW_CONTAINERS
            and normalized_container in CONTAINER_TIMEOUT_PRONE_MAIN_FLOW
            and range_ns > fallback_first_threshold_ns
        ):
            return self._request_id_set_from_container_fallback(filters, normalized_container)

        try:
            return self._collect_container_request_ids_chunked(
                container=normalized_container,
                start_ns=start_ns,
                end_ns=end_ns,
            )
        except RuntimeError as exc:
            if not is_retryable_loki_error(exc):
                raise
            return self._request_id_set_from_container_fallback(filters, normalized_container)

    def _build_request_match(self, filters: FilterOptions, include_container: bool = False) -> dict:
        match = build_request_match(filters)
        normalized_container: str | None = None
        effective_tool = str(filters.tool or "").strip() or None

        if include_container and filters.container:
            normalized_container = self._assert_container(filters.container)
            effective_tool = enforce_container_tool(normalized_container, effective_tool)

        request_id_sets = []
        if filters.request_id:
            request_id_sets.append({filters.request_id})
        if filters.agent_code:
            request_id_sets.append(self._request_id_set_from_agents(filters))
        if effective_tool:
            request_id_sets.append(
                self._request_id_set_from_tools(replace(filters, tool=effective_tool))
            )
        if (
            include_container
            and normalized_container
            and self._should_filter_container_with_loki(normalized_container)
        ):
            request_id_sets.append(
                self._request_id_set_from_container(
                    replace(filters, container=normalized_container)
                )
            )
            mapped_tool = mapped_tool_for_container(normalized_container)
            if mapped_tool and mapped_tool != effective_tool:
                request_id_sets.append(
                    self._request_id_set_from_tools(
                        replace(filters, container=normalized_container, tool=mapped_tool)
                    )
                )

        if request_id_sets:
            valid_request_ids = request_id_sets[0]
            for next_set in request_id_sets[1:]:
                valid_request_ids = valid_request_ids.intersection(next_set)

            if not valid_request_ids:
                return {"request_id": {"$exists": False}}
            match["request_id"] = {"$in": sorted(valid_request_ids)}

        return match

    def _tool_call_count_map(self, request_ids: Iterable[str]) -> dict[str, int]:
        ids = [request_id for request_id in request_ids if request_id]
        if not ids:
            return {}

        pipeline = [
            {"$match": {"request_id": {"$in": ids}}},
            {"$group": {"_id": "$request_id", "count": {"$sum": 1}}},
        ]
        return {
            item["_id"]: int(item.get("count", 0))
            for item in self.tool_collection.aggregate(pipeline)
        }

    def get_overview(self, filters: FilterOptions) -> dict:
        match = self._build_request_match(filters)
        docs = self.request_repo.find(match, OVERVIEW_PROJECTION)
        request_ids = [doc.get("request_id") for doc in docs if doc.get("request_id")]
        tool_count_map = self._tool_call_count_map(request_ids)
        return build_overview_payload(docs, tool_count_map)

    def get_trends(self, filters: FilterOptions, granularity: str) -> list[dict]:
        match = self._build_request_match(filters)
        unit = "day" if granularity == "day" else "hour"

        pipeline = [
            {"$match": match},
            {
                "$project": {
                    "bucket": {
                        "$dateTrunc": {
                            "date": "$start_ts",
                            "unit": unit,
                            "timezone": self.settings.timezone,
                        }
                    },
                    "status": 1,
                    "duration_s": {"$ifNull": ["$duration_s", 0]},
                    "token_total": {"$ifNull": ["$token_usage_total.total.total_tokens", 0]},
                }
            },
            {
                "$group": {
                    "_id": "$bucket",
                    "total_requests": {"$sum": 1},
                    "success_requests": {
                        "$sum": {"$cond": [{"$eq": [{"$toLower": "$status"}, "success"]}, 1, 0]}
                    },
                    "avg_duration_s": {"$avg": "$duration_s"},
                    "token_total": {"$sum": "$token_total"},
                }
            },
            {"$sort": {"_id": 1}},
        ]

        items = self.request_repo.aggregate(pipeline)
        return build_trends_rows(items)

    def get_users(self, filters: FilterOptions, top_n: int) -> list[dict]:
        match = self._build_request_match(filters)
        docs = self.request_repo.find(match, USERS_PROJECTION)
        request_ids = [doc.get("request_id") for doc in docs if doc.get("request_id")]
        tool_count_map = self._tool_call_count_map(request_ids)
        return build_users_rows(docs, tool_count_map, top_n)

    def _group_agent_executions(self, filters: FilterOptions, request_ids: list[str]) -> list[dict]:
        if not request_ids:
            return []

        match = build_agent_match(filters)
        match["request_id"] = {"$in": request_ids}

        pipeline = [
            {"$match": match},
            {"$sort": {"ts": 1}},
            {
                "$project": {
                    "request_id": 1,
                    "state_id": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "status": 1,
                    "ts": 1,
                    "start_ts": {"$cond": [{"$eq": ["$stage", "start"]}, "$ts", None]},
                    "end_ts": {"$cond": [{"$eq": ["$stage", "end"]}, "$ts", None]},
                }
            },
            {
                "$group": {
                    "_id": {
                        "request_id": "$request_id",
                        "state_id": "$state_id",
                        "agent_code": "$agent_code",
                    },
                    "request_id": {"$first": "$request_id"},
                    "agent_code": {"$first": "$agent_code"},
                    "agent_name": {"$last": "$agent_name"},
                    "start_ts": {"$min": "$start_ts"},
                    "end_ts": {"$max": "$end_ts"},
                    "first_ts": {"$min": "$ts"},
                    "last_ts": {"$max": "$ts"},
                    "status": {"$last": "$status"},
                }
            },
        ]

        return list(self.agent_collection.aggregate(pipeline))

    def get_agents(self, filters: FilterOptions, top_n: int) -> list[dict]:
        request_match = self._build_request_match(filters)
        request_docs = self.request_repo.find(
            request_match, {"_id": 0, "request_id": 1, "status": 1}
        )
        request_status_map = {
            doc.get("request_id"): str(doc.get("status", "")).lower()
            for doc in request_docs
            if doc.get("request_id")
        }

        executions = self._group_agent_executions(filters, list(request_status_map.keys()))
        rows = build_agents_rows(
            executions, request_status_map, self.settings.slow_call_threshold_s
        )
        return rows[:top_n]

    def get_tools(self, filters: FilterOptions, top_n: int) -> dict:
        request_match = self._build_request_match(filters)
        request_docs = self.request_repo.find(
            request_match, {"_id": 0, "request_id": 1, "duration_s": 1}
        )
        request_duration_map = {
            doc.get("request_id"): to_float(doc.get("duration_s"), 0.0)
            for doc in request_docs
            if doc.get("request_id")
        }
        request_ids = list(request_duration_map.keys())

        if not request_ids:
            return {"items": [], "failure_top": []}

        tool_match = build_tool_match(filters)
        tool_match["request_id"] = {"$in": request_ids}

        tool_docs = self.agent_tool_repo.find_tools(
            tool_match,
            {"_id": 0, "tool": 1, "status": 1, "request_id": 1, "duration_s": 1},
        )
        return build_tools_payload(tool_docs, request_duration_map, top_n)

    def get_llm_calls(self, filters: FilterOptions, top_n: int = 200) -> dict:
        request_match = self._build_request_match(filters)
        request_ids = [
            doc.get("request_id")
            for doc in self.request_repo.find(request_match, {"request_id": 1})
            if doc.get("request_id")
        ]
        if not request_ids:
            return EMPTY_LLM_CALLS_PAYLOAD

        items = self.llm_repo.find_for_request_ids(request_ids, filters.agent_code, top_n)
        return build_llm_calls_payload(items)

    def list_requests(
        self,
        filters: FilterOptions,
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
    ) -> dict:
        match = self._build_request_match(filters, include_container=True)
        total = self.request_repo.count(match)

        sortable_fields = {"start_ts", "end_ts", "duration_s", "status", "staff_code", "request_id"}
        normalized_sort_by = sort_by if sort_by in sortable_fields else "start_ts"
        normalized_sort_order = -1 if str(sort_order).lower() == "desc" else 1

        docs = self.request_repo.find(
            match,
            LIST_PROJECTION,
            sort=[normalized_sort_by, normalized_sort_order],
            skip=(page - 1) * page_size,
            limit=page_size,
        )

        request_ids = [doc.get("request_id") for doc in docs if doc.get("request_id")]
        tool_count_map = self._tool_call_count_map(request_ids)
        items = build_request_items(docs, tool_count_map)

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    def iter_request_export_jsonl(
        self,
        filters: FilterOptions,
        request_ids: Sequence[str] | None = None,
        sort_by: str = "start_ts",
        sort_order: str = "desc",
    ) -> Iterator[str]:
        match = self._build_request_match(filters, include_container=True)
        selected_ids = [str(item).strip() for item in (request_ids or []) if str(item).strip()]
        if selected_ids:
            selected_set = set(selected_ids)
            request_filter = match.get("request_id")
            if isinstance(request_filter, dict) and "$in" in request_filter:
                selected_set = selected_set.intersection(
                    {str(item) for item in request_filter.get("$in", [])}
                )
            elif request_filter:
                selected_set = selected_set.intersection({str(request_filter)})
            match["request_id"] = {"$in": sorted(selected_set)}

        sortable_fields = {"start_ts", "end_ts", "duration_s", "status", "staff_code", "request_id"}
        normalized_sort_by = sort_by if sort_by in sortable_fields else "start_ts"
        normalized_sort_order = -1 if str(sort_order).lower() == "desc" else 1

        def generate() -> Iterator[str]:
            cursor = self.request_collection.find(match, {"_id": 0, "request_id": 1}).sort(
                normalized_sort_by,
                normalized_sort_order,
            )
            for doc in cursor:
                request_id = doc.get("request_id")
                if not request_id:
                    continue
                try:
                    detail = self.get_request_detail(str(request_id))
                except KeyError:
                    continue
                yield json.dumps(detail, ensure_ascii=False, default=json_default) + "\n"

        return generate()

    def get_request_detail(self, request_id: str) -> dict:
        request_doc = self.request_repo.find_one({"request_id": request_id}, DETAIL_PROJECTION)
        if not request_doc:
            raise KeyError(f"request_id={request_id} not found")

        agent_events = self.agent_tool_repo.agent_events_for_request(request_id)
        tool_calls_raw = self.agent_tool_repo.tool_calls_raw_for_request(request_id)
        tool_calls = merge_tool_call_rows(tool_calls_raw)
        llm_calls = self.llm_repo.find_for_request(request_id)

        timeline = build_agent_timeline(agent_events)

        return {
            "request": build_request_payload(request_doc),
            "agent_timeline": timeline,
            "agent_events": agent_events,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "summary": {
                "agent_event_count": len(agent_events),
                "tool_call_count": len(tool_calls),
                "tool_call_raw_count": len(tool_calls_raw),
                "llm_call_count": len(llm_calls),
            },
        }
