from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from pymongo.database import Database

from app.core.config import Settings
from app.core.database import MongoCollections
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
from app.services.log_parser import parse_log_context, resolve_correlation_id
from app.services.loki_query_service import LokiQueryService
from app.services.math_utils import average, compact_confidences, percentile, safe_div, to_float

EXCLUDED_AGENT_CODES_FOR_DURATION = {"globaldomainorchestrator"}
LOKI_QUERY_LIMIT = 1000
MIN_SPLIT_WINDOW_SECONDS = 60
CONTAINER_QUERY_CHUNK_SECONDS = 6 * 60 * 60
CONTAINER_REQUEST_VERIFY_WINDOW_SECONDS = 120
MAX_CONTAINER_FALLBACK_CANDIDATES = 500
CONTAINER_TIMEOUT_PRONE_MAIN_FLOW = {"map_core-test", "map_core-preprod"}
CONTAINER_FALLBACK_FIRST_RANGE_HOURS = 24


class AnalyticsService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        collections: Optional[MongoCollections] = None,
        loki_query_service: Optional[LokiQueryService] = None,
        trusted_container_filters: Optional[Iterable[str]] = None,
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

    def _to_token_total(self, doc: Dict) -> float:
        usage = doc.get("token_usage_total") or {}
        total = usage.get("total") if isinstance(usage, dict) else {}
        if not isinstance(total, dict):
            total = {}
        return to_float(total.get("total_tokens"), 0.0)

    def _to_scene_confidences(self, doc: Dict) -> Dict[str, List[float]]:
        scene_result = doc.get("scene_result") or {}
        big_scenes = scene_result.get("big_scenes") if isinstance(scene_result, dict) else []
        sub_scenes = scene_result.get("sub_scenes") if isinstance(scene_result, dict) else []
        if not isinstance(big_scenes, list):
            big_scenes = []
        if not isinstance(sub_scenes, list):
            sub_scenes = []

        big_conf = compact_confidences(
            (item.get("confidence") for item in big_scenes if isinstance(item, dict))
        )
        sub_conf = compact_confidences(
            (item.get("confidence") for item in sub_scenes if isinstance(item, dict))
        )
        return {"big": big_conf, "sub": sub_conf}

    def _tool_status(self, raw_status: Optional[str]) -> str:
        if raw_status is None:
            return "unknown"

        status = str(raw_status).strip().lower()
        if not status:
            return "unknown"
        if status in {"success", "ok", "done"}:
            return "success"
        return "failed"

    def _request_id_set_from_agents(self, filters: FilterOptions) -> set:
        match = build_agent_match(filters)
        cursor = self.agent_collection.find(match, {"request_id": 1})
        return {doc.get("request_id") for doc in cursor if doc.get("request_id")}

    def _request_id_set_from_tools(self, filters: FilterOptions) -> set:
        match = build_tool_match(filters)
        cursor = self.tool_collection.find(match, {"request_id": 1})
        return {doc.get("request_id") for doc in cursor if doc.get("request_id")}

    @staticmethod
    def _assert_container(container: str) -> str:
        return assert_container_supported(container)

    def _should_filter_container_with_loki(self, container: str) -> bool:
        if self.loki_query_service is None or not self.loki_query_service.is_enabled():
            return False
        return container not in self.trusted_container_filters

    @staticmethod
    def _to_ns(value: datetime) -> int:
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000_000)

    @staticmethod
    def _extract_request_ids_from_rows(rows: List[Dict]) -> set[str]:
        result: set[str] = set()
        for row in rows:
            line = str(row.get("line") or "")
            stream = row.get("stream") if isinstance(row.get("stream"), dict) else {}
            parsed = parse_log_context(line, stream=stream)
            resolved = resolve_correlation_id(parsed)
            resolved_id = resolved.get("id_value")
            if resolved_id:
                result.add(str(resolved_id))
        return result

    @staticmethod
    def _is_retryable_loki_error(exc: RuntimeError) -> bool:
        detail = str(exc).strip().lower()
        return any(
            token in detail
            for token in (
                "timed out",
                "timeout",
                "http 502",
                "http 503",
                "http 504",
            )
        )

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

    def _request_id_set_from_container_fallback(self, filters: FilterOptions, container: str) -> set[str]:
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
            raw_start = doc.get("start_ts") if isinstance(doc.get("start_ts"), datetime) else filters.start_ts
            raw_end = doc.get("end_ts") if isinstance(doc.get("end_ts"), datetime) else raw_start
            start_ns = max(0, self._to_ns(raw_start) - verify_window_ns)
            end_ns = max(start_ns, self._to_ns(raw_end) + verify_window_ns)
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
            if window_ns > min_split_window_ns and self._is_retryable_loki_error(exc):
                return self._collect_container_request_ids_split(
                    container=container,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
            raise
        request_ids = self._extract_request_ids_from_rows(rows)

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

        start_ns = self._to_ns(filters.start_ts)
        end_ns = self._to_ns(filters.end_ts)
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
            if not self._is_retryable_loki_error(exc):
                raise
            return self._request_id_set_from_container_fallback(filters, normalized_container)

    def _build_request_match(self, filters: FilterOptions, include_container: bool = False) -> Dict:
        match = build_request_match(filters)
        normalized_container: Optional[str] = None
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
            request_id_sets.append(self._request_id_set_from_tools(replace(filters, tool=effective_tool)))
        if (
            include_container
            and normalized_container
            and self._should_filter_container_with_loki(normalized_container)
        ):
            request_id_sets.append(self._request_id_set_from_container(replace(filters, container=normalized_container)))
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

    def _tool_call_count_map(self, request_ids: Iterable[str]) -> Dict[str, int]:
        ids = [request_id for request_id in request_ids if request_id]
        if not ids:
            return {}

        pipeline = [
            {"$match": {"request_id": {"$in": ids}}},
            {"$group": {"_id": "$request_id", "count": {"$sum": 1}}},
        ]
        return {item["_id"]: int(item.get("count", 0)) for item in self.tool_collection.aggregate(pipeline)}

    def get_overview(self, filters: FilterOptions) -> Dict:
        match = self._build_request_match(filters)
        docs = list(
            self.request_collection.find(
                match,
                {
                    "_id": 0,
                    "request_id": 1,
                    "status": 1,
                    "duration_s": 1,
                    "token_usage_total": 1,
                    "scene_result": 1,
                },
            )
        )

        total_requests = len(docs)
        success_requests = sum(1 for doc in docs if str(doc.get("status", "")).lower() == "success")
        error_requests = total_requests - success_requests

        durations = [to_float(doc.get("duration_s"), 0.0) for doc in docs if doc.get("duration_s") is not None]
        token_totals = [self._to_token_total(doc) for doc in docs]

        request_ids = [doc.get("request_id") for doc in docs if doc.get("request_id")]
        tool_count_map = self._tool_call_count_map(request_ids)
        total_tool_calls = sum(tool_count_map.values())

        all_big_conf = []
        all_sub_conf = []
        for doc in docs:
            confidence_map = self._to_scene_confidences(doc)
            all_big_conf.extend(confidence_map["big"])
            all_sub_conf.extend(confidence_map["sub"])

        return {
            "total_requests": total_requests,
            "success_requests": success_requests,
            "success_rate": safe_div(success_requests, total_requests),
            "error_rate": safe_div(error_requests, total_requests),
            "duration_s": {
                "avg": average(durations),
                "p50": percentile(durations, 0.50),
                "p90": percentile(durations, 0.90),
                "p95": percentile(durations, 0.95),
                "max": max(durations) if durations else 0.0,
            },
            "token": {
                "total": sum(token_totals),
                "avg_per_request": safe_div(sum(token_totals), total_requests),
                "efficiency_per_success_request": safe_div(sum(token_totals), success_requests),
            },
            "tool_calls": {
                "total": total_tool_calls,
                "per_request": safe_div(total_tool_calls, total_requests),
            },
            "scene_confidence_avg": {
                "big_scene": average(all_big_conf),
                "sub_scene": average(all_sub_conf),
            },
        }

    def get_trends(self, filters: FilterOptions, granularity: str) -> List[Dict]:
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
                        "$sum": {
                            "$cond": [{"$eq": [{"$toLower": "$status"}, "success"]}, 1, 0]
                        }
                    },
                    "avg_duration_s": {"$avg": "$duration_s"},
                    "token_total": {"$sum": "$token_total"},
                }
            },
            {"$sort": {"_id": 1}},
        ]

        rows = []
        for item in self.request_collection.aggregate(pipeline):
            total_requests = int(item.get("total_requests", 0))
            success_requests = int(item.get("success_requests", 0))
            rows.append(
                {
                    "bucket_ts": item.get("_id"),
                    "total_requests": total_requests,
                    "success_rate": safe_div(success_requests, total_requests),
                    "avg_duration_s": to_float(item.get("avg_duration_s"), 0.0),
                    "token_total": to_float(item.get("token_total"), 0.0),
                }
            )

        return rows

    def get_users(self, filters: FilterOptions, top_n: int) -> List[Dict]:
        match = self._build_request_match(filters)
        docs = list(
            self.request_collection.find(
                match,
                {
                    "_id": 0,
                    "request_id": 1,
                    "staff_code": 1,
                    "status": 1,
                    "duration_s": 1,
                    "token_usage_total": 1,
                },
            )
        )

        user_map = defaultdict(
            lambda: {
                "request_count": 0,
                "success_count": 0,
                "durations": [],
                "token_total": 0.0,
                "request_ids": [],
            }
        )

        for doc in docs:
            staff_code = doc.get("staff_code") or "UNKNOWN"
            user = user_map[staff_code]
            user["request_count"] += 1
            if str(doc.get("status", "")).lower() == "success":
                user["success_count"] += 1
            user["durations"].append(to_float(doc.get("duration_s"), 0.0))
            user["token_total"] += self._to_token_total(doc)
            if doc.get("request_id"):
                user["request_ids"].append(doc["request_id"])

        request_ids = [doc.get("request_id") for doc in docs if doc.get("request_id")]
        tool_count_map = self._tool_call_count_map(request_ids)

        rows = []
        for staff_code, info in user_map.items():
            request_count = info["request_count"]
            tool_calls = sum(tool_count_map.get(request_id, 0) for request_id in info["request_ids"])
            rows.append(
                {
                    "staff_code": staff_code,
                    "request_count": request_count,
                    "success_rate": safe_div(info["success_count"], request_count),
                    "avg_duration_s": average(info["durations"]),
                    "p95_duration_s": percentile(info["durations"], 0.95),
                    "token_total": info["token_total"],
                    "tool_calls_per_request": safe_div(tool_calls, request_count),
                }
            )

        rows.sort(key=lambda item: item["request_count"], reverse=True)
        return rows[:top_n]

    def _group_agent_executions(self, filters: FilterOptions, request_ids: List[str]) -> List[Dict]:
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

    def get_agents(self, filters: FilterOptions, top_n: int) -> List[Dict]:
        request_match = self._build_request_match(filters)
        request_docs = list(
            self.request_collection.find(request_match, {"_id": 0, "request_id": 1, "status": 1})
        )
        request_status_map = {
            doc.get("request_id"): str(doc.get("status", "")).lower() for doc in request_docs if doc.get("request_id")
        }

        executions = self._group_agent_executions(filters, list(request_status_map.keys()))

        grouped = defaultdict(
            lambda: {
                "agent_name": "",
                "call_count": 0,
                "success_count": 0,
                "durations": [],
                "slow_count": 0,
            }
        )

        for item in executions:
            agent_code = item.get("agent_code") or "UNKNOWN"
            if str(agent_code).strip().lower() in EXCLUDED_AGENT_CODES_FOR_DURATION:
                continue
            group = grouped[agent_code]
            group["agent_name"] = item.get("agent_name") or agent_code
            group["call_count"] += 1

            request_status = request_status_map.get(item.get("request_id"), "")
            if request_status == "success":
                group["success_count"] += 1

            start_ts = item.get("start_ts")
            end_ts = item.get("end_ts")
            first_ts = item.get("first_ts")
            last_ts = item.get("last_ts")
            duration_s = 0.0
            if isinstance(start_ts, datetime) and isinstance(end_ts, datetime):
                duration_s = max((end_ts - start_ts).total_seconds(), 0.0)
            elif isinstance(first_ts, datetime) and isinstance(last_ts, datetime):
                duration_s = max((last_ts - first_ts).total_seconds(), 0.0)

            group["durations"].append(duration_s)
            if duration_s >= self.settings.slow_call_threshold_s:
                group["slow_count"] += 1

        rows = []
        for agent_code, info in grouped.items():
            call_count = info["call_count"]
            rows.append(
                {
                    "agent_code": agent_code,
                    "agent_name": info["agent_name"],
                    "call_count": call_count,
                    "success_rate": safe_div(info["success_count"], call_count),
                    "avg_duration_s": average(info["durations"]),
                    "slow_call_ratio": safe_div(info["slow_count"], call_count),
                }
            )

        rows.sort(key=lambda item: item["call_count"], reverse=True)
        return rows[:top_n]

    def get_tools(self, filters: FilterOptions, top_n: int) -> Dict:
        request_match = self._build_request_match(filters)
        request_docs = list(
            self.request_collection.find(request_match, {"_id": 0, "request_id": 1, "duration_s": 1})
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

        tool_docs = list(
            self.tool_collection.find(
                tool_match,
                {
                    "_id": 0,
                    "tool": 1,
                    "status": 1,
                    "request_id": 1,
                    "duration_s": 1,
                },
            )
        )

        grouped = defaultdict(
            lambda: {
                "call_count": 0,
                "known_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "explicit_duration": [],
                "proxy_duration_sum": 0.0,
                "proxy_duration_count": 0,
            }
        )

        for doc in tool_docs:
            tool_name = doc.get("tool") or "UNKNOWN"
            group = grouped[tool_name]
            group["call_count"] += 1

            normalized_status = self._tool_status(doc.get("status"))
            if normalized_status != "unknown":
                group["known_count"] += 1
                if normalized_status == "success":
                    group["success_count"] += 1
                else:
                    group["failed_count"] += 1

            explicit_duration = doc.get("duration_s")
            if explicit_duration is not None:
                group["explicit_duration"].append(to_float(explicit_duration, 0.0))
            else:
                request_duration = request_duration_map.get(doc.get("request_id"), 0.0)
                group["proxy_duration_sum"] += request_duration
                group["proxy_duration_count"] += 1

        items = []
        for tool_name, info in grouped.items():
            if info["explicit_duration"]:
                avg_duration = average(info["explicit_duration"])
            else:
                avg_duration = safe_div(info["proxy_duration_sum"], info["proxy_duration_count"])

            items.append(
                {
                    "tool": tool_name,
                    "call_count": info["call_count"],
                    "success_rate": safe_div(info["success_count"], info["known_count"]),
                    "avg_duration_s": avg_duration,
                    "failed_count": info["failed_count"],
                }
            )

        items.sort(key=lambda item: item["call_count"], reverse=True)
        failure_top = sorted(items, key=lambda item: item["failed_count"], reverse=True)[:top_n]

        return {
            "items": items[:top_n],
            "failure_top": failure_top,
        }

    def get_llm_calls(self, filters: FilterOptions, top_n: int = 200) -> Dict:
        request_match = self._build_request_match(filters)
        request_ids = [
            doc.get("request_id")
            for doc in self.request_collection.find(request_match, {"request_id": 1})
            if doc.get("request_id")
        ]
        if not request_ids:
            return {
                "items": [],
                "summary": {
                    "call_count": 0,
                    "failed_count": 0,
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "avg_duration_s": 0.0,
                    "p95_duration_s": 0.0,
                    "token_total": 0,
                },
            }

        match: Dict[str, Any] = {"request_id": {"$in": request_ids}}
        if filters.agent_code:
            match["agent_code"] = filters.agent_code
        cursor = self.llm_collection.find(
            match,
            {
                "_id": 0,
                "state_id": 1,
                "request_id": 1,
                "session_id": 1,
                "staff_code": 1,
                "seq": 1,
                "agent_code": 1,
                "agent_name": 1,
                "component": 1,
                "phase": 1,
                "step": 1,
                "call_kind": 1,
                "model": 1,
                "provider_request_id": 1,
                "start_ts": 1,
                "end_ts": 1,
                "duration_s": 1,
                "status": 1,
                "usage": 1,
                "error": 1,
                "finish_reason": 1,
                "prompt_summary": 1,
                "tool_names": 1,
            },
        ).sort([("start_ts", -1), ("seq", -1)]).limit(top_n)
        items = list(cursor)
        failed_count = sum(1 for item in items if str(item.get("status")) != "success")
        durations = [
            to_float(item.get("duration_s"), 0.0)
            for item in items
            if item.get("duration_s") is not None
        ]
        token_total = 0
        for item in items:
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            token_total += int(
                to_float(
                    usage.get("total_tokens")
                    or usage.get("total")
                    or usage.get("completion_tokens")
                    or 0,
                    0.0,
                )
            )
        call_count = len(items)
        return {
            "items": items,
            "summary": {
                "call_count": call_count,
                "failed_count": failed_count,
                "total": call_count,
                "success": call_count - failed_count,
                "failed": failed_count,
                "avg_duration_s": average(durations),
                "p95_duration_s": percentile(durations, 95),
                "token_total": token_total,
            },
        }

    def list_requests(
        self,
        filters: FilterOptions,
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
    ) -> Dict:
        match = self._build_request_match(filters, include_container=True)
        total = self.request_collection.count_documents(match)

        sortable_fields = {"start_ts", "end_ts", "duration_s", "status", "staff_code", "request_id"}
        normalized_sort_by = sort_by if sort_by in sortable_fields else "start_ts"
        normalized_sort_order = -1 if str(sort_order).lower() == "desc" else 1

        cursor = (
            self.request_collection.find(
                match,
                {
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
                },
            )
            .sort(normalized_sort_by, normalized_sort_order)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        items = []
        docs = list(cursor)
        request_ids = [doc.get("request_id") for doc in docs if doc.get("request_id")]
        tool_count_map = self._tool_call_count_map(request_ids)

        for doc in docs:
            request_id = doc.get("request_id")
            items.append(
                {
                    "request_id": request_id,
                    "session_id": doc.get("session_id"),
                    "staff_code": doc.get("staff_code"),
                    "status": doc.get("status"),
                    "duration_s": to_float(doc.get("duration_s"), 0.0),
                    "start_ts": doc.get("start_ts"),
                    "end_ts": doc.get("end_ts"),
                    "query": doc.get("query"),
                    "agents_called": doc.get("agents_called") or [],
                    "token_total": self._to_token_total(doc),
                    "tool_call_count": tool_count_map.get(request_id, 0),
                }
            )

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    def iter_request_export_jsonl(
        self,
        filters: FilterOptions,
        request_ids: Optional[Sequence[str]] = None,
        sort_by: str = "start_ts",
        sort_order: str = "desc",
    ) -> Iterator[str]:
        match = self._build_request_match(filters, include_container=True)
        selected_ids = [str(item).strip() for item in (request_ids or []) if str(item).strip()]
        if selected_ids:
            selected_set = set(selected_ids)
            request_filter = match.get("request_id")
            if isinstance(request_filter, dict) and "$in" in request_filter:
                selected_set = selected_set.intersection({str(item) for item in request_filter.get("$in", [])})
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
                yield json.dumps(detail, ensure_ascii=False, default=self._json_default) + "\n"

        return generate()

    def _build_agent_timeline(self, events: List[Dict]) -> List[Dict]:
        timeline: List[Dict] = []
        open_items: Dict[tuple, Dict] = {}

        for event in events:
            key = (
                event.get("state_id"),
                event.get("request_id"),
                event.get("agent_code"),
                event.get("event_type"),
                event.get("component"),
            )

            stage = str(event.get("stage") or "").lower()
            ts = event.get("ts")
            item = {
                "state_id": event.get("state_id"),
                "request_id": event.get("request_id"),
                "agent_code": event.get("agent_code"),
                "agent_name": event.get("agent_name"),
                "seq": event.get("seq", 0),
                "event_type": event.get("event_type"),
                "component": event.get("component"),
                "start_ts": None,
                "end_ts": None,
                "status": None,
            }

            if stage == "start":
                item["start_ts"] = ts
                open_items[key] = item
                timeline.append(item)
                continue

            if stage == "end":
                item = open_items.pop(key, item)
                if item not in timeline:
                    timeline.append(item)
                item["end_ts"] = ts

            if event.get("status") is not None:
                item["status"] = event.get("status")

            if stage not in {"start", "end"} and event.get("event_type") == "token_usage":
                continue
            if stage not in {"start", "end"} and item not in timeline:
                timeline.append(item)

        for item in timeline:
            start_ts = item.get("start_ts")
            end_ts = item.get("end_ts")
            duration_s = 0.0
            if isinstance(start_ts, datetime) and isinstance(end_ts, datetime):
                duration_s = max((end_ts - start_ts).total_seconds(), 0.0)

            item["duration_s"] = duration_s

        min_utc = datetime.min.replace(tzinfo=timezone.utc)
        timeline.sort(key=lambda row: ((row.get("start_ts") or row.get("end_ts") or min_utc), row.get("seq", 0)))
        return timeline

    @staticmethod
    def _to_utc_dt(value: object) -> Optional[datetime]:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                return None
        return None

    @staticmethod
    def _json_default(value: object) -> str:
        if isinstance(value, datetime):
            dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return str(value)

    @staticmethod
    def _tool_call_identity(row: Dict) -> Tuple[str, str, str, int]:
        agent_code = str(row.get("agent_code") or "unknown_agent")
        tool = str(row.get("tool") or "unknown_tool")
        tool_id = str(row.get("tool_id") or "unknown_id")
        step_raw = row.get("step")
        try:
            step = int(step_raw) if step_raw is not None else -1
        except (TypeError, ValueError):
            step = -1
        return agent_code, tool, tool_id, step

    def _merge_tool_call_rows(self, rows: List[Dict]) -> List[Dict]:
        merged: Dict[Tuple[str, str, str, int], Dict] = {}

        for row in rows:
            key = self._tool_call_identity(row)
            current = merged.get(key)
            if current is None:
                merged[key] = dict(row)
                continue

            status = row.get("status")
            if status not in (None, ""):
                current["status"] = status

            if current.get("args") is None and row.get("args") is not None:
                current["args"] = row.get("args")
            if current.get("output") is None and row.get("output") is not None:
                current["output"] = row.get("output")

            if row.get("duration_s") is not None:
                duration = to_float(row.get("duration_s"))
                current["duration_s"] = duration

            row_ts = self._to_utc_dt(row.get("ts"))
            cur_ts = self._to_utc_dt(current.get("ts"))
            if row_ts and (cur_ts is None or row_ts < cur_ts):
                current["ts"] = row.get("ts")

            row_end_ts = self._to_utc_dt(row.get("end_ts")) or self._to_utc_dt(row.get("ts"))
            cur_end_ts = self._to_utc_dt(current.get("end_ts")) or self._to_utc_dt(current.get("ts"))
            if row_end_ts and (cur_end_ts is None or row_end_ts > cur_end_ts):
                current["end_ts"] = row.get("end_ts") or row.get("ts")

        merged_rows = list(merged.values())
        min_utc = datetime.min.replace(tzinfo=timezone.utc)
        merged_rows.sort(key=lambda item: (self._to_utc_dt(item.get("ts")) or min_utc, self._tool_call_identity(item)))
        return merged_rows

    def get_request_detail(self, request_id: str) -> Dict:
        request_doc = self.request_collection.find_one(
            {"request_id": request_id},
            {
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
            },
        )
        if not request_doc:
            raise KeyError(f"request_id={request_id} not found")

        agent_events = list(
            self.agent_collection.find(
                {"request_id": request_id},
                {
                    "_id": 0,
                    "state_id": 1,
                    "request_id": 1,
                    "session_id": 1,
                    "staff_code": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "seq": 1,
                    "event_type": 1,
                    "component": 1,
                    "stage": 1,
                    "status": 1,
                    "payload": 1,
                    "ts": 1,
                },
            ).sort("ts", 1)
        )

        tool_calls_raw = list(
            self.tool_collection.find(
                {"request_id": request_id},
                {
                    "_id": 0,
                    "event_type": 1,
                    "state_id": 1,
                    "request_id": 1,
                    "session_id": 1,
                    "ts": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "agent_id": 1,
                    "tool": 1,
                    "tool_id": 1,
                    "step": 1,
                    "args": 1,
                    "output": 1,
                    "status": 1,
                    "duration_s": 1,
                },
            ).sort("ts", 1)
        )
        tool_calls = self._merge_tool_call_rows(tool_calls_raw)
        llm_calls = list(
            self.llm_collection.find(
                {"request_id": request_id},
                {
                    "_id": 0,
                    "state_id": 1,
                    "request_id": 1,
                    "session_id": 1,
                    "staff_code": 1,
                    "seq": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "component": 1,
                    "phase": 1,
                    "step": 1,
                    "call_kind": 1,
                    "model": 1,
                    "provider_request_id": 1,
                    "start_ts": 1,
                    "end_ts": 1,
                    "duration_s": 1,
                    "status": 1,
                    "usage": 1,
                    "error": 1,
                    "finish_reason": 1,
                    "prompt_summary": 1,
                    "tool_names": 1,
                },
            ).sort([("start_ts", 1), ("seq", 1)])
        )

        timeline = self._build_agent_timeline(agent_events)

        return {
            "request": {
                **request_doc,
                "duration_s": to_float(request_doc.get("duration_s"), 0.0),
                "token_total": self._to_token_total(request_doc),
            },
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
