from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from pymongo.database import Database

from app.core.database import MongoCollections
from app.services.container_mapping import (
    assert_container_supported,
    enforce_container_tool,
    infer_cbb_container_by_tool,
    infer_main_flow_container,
)
from app.services.log_parser import normalize_levels, parse_log_context, resolve_correlation_id
from app.services.loki_query_service import LokiQueryService
from app.services.math_utils import to_float
from app.services.time_align_service import AlignedRange, TimeAlignService

RID_PATTERN = re.compile(r"rid=([A-Za-z0-9_-]{8,128})")
SID_PATTERN = re.compile(r"sid=([A-Za-z0-9_-]{6,128})")
ERROR_KEYWORDS = ("error", "exception", "failed", "traceback", "timeout")
ALERT_LEVELS = {"ERROR", "WARNING"}
ERROR_TYPE_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Exception|Error))\b")

MAX_PAGE_SIZE = 10
DEFAULT_PAGE_SIZE = 10


class CorrelationService:
    def __init__(
        self,
        database: Database,
        collections: MongoCollections,
        time_align_service: TimeAlignService,
        loki_query_service: LokiQueryService,
    ) -> None:
        self.database = database
        self.collections = collections
        self.time_align_service = time_align_service
        self.loki_query_service = loki_query_service
        self.request_collection = database[collections.request_records]
        self.agent_collection = database[collections.agent_executions]
        self.tool_collection = database[collections.tool_call_records]

    @staticmethod
    def _assert_container(container: str) -> str:
        return assert_container_supported(container)

    @staticmethod
    def _dt(value: Optional[datetime]) -> Optional[datetime]:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _token_total(request_doc: Dict[str, Any]) -> float:
        usage = request_doc.get("token_usage_total") or {}
        total = usage.get("total") if isinstance(usage, dict) else {}
        if not isinstance(total, dict):
            total = {}
        return to_float(total.get("total_tokens"), 0.0)

    @staticmethod
    def _build_timeline(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[Any, Dict[str, Any]] = {}
        for event in events:
            key = (event.get("state_id"), event.get("agent_code"), event.get("seq", 0))
            row = grouped.setdefault(
                key,
                {
                    "state_id": event.get("state_id"),
                    "agent_code": event.get("agent_code"),
                    "agent_name": event.get("agent_name"),
                    "seq": event.get("seq", 0),
                    "status": event.get("status"),
                    "start_ts": None,
                    "end_ts": None,
                },
            )
            stage = str(event.get("stage") or "").lower()
            ts = event.get("ts")
            if stage == "start" and isinstance(ts, datetime):
                if row["start_ts"] is None or ts < row["start_ts"]:
                    row["start_ts"] = ts
            if stage == "end" and isinstance(ts, datetime):
                if row["end_ts"] is None or ts > row["end_ts"]:
                    row["end_ts"] = ts
            if event.get("status") is not None:
                row["status"] = event.get("status")

        timeline: List[Dict[str, Any]] = []
        for row in grouped.values():
            start_ts = row.get("start_ts")
            end_ts = row.get("end_ts")
            duration_s = 0.0
            if isinstance(start_ts, datetime) and isinstance(end_ts, datetime):
                duration_s = max((end_ts - start_ts).total_seconds(), 0.0)
            timeline.append(
                {
                    **row,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "duration_s": duration_s,
                }
            )

        min_utc = datetime.min.replace(tzinfo=timezone.utc)
        timeline.sort(key=lambda item: (item.get("start_ts") or min_utc, item.get("seq", 0)))
        return timeline

    @staticmethod
    def _extract_rid(line: str) -> Optional[str]:
        match = RID_PATTERN.search(line or "")
        return match.group(1) if match else None

    @staticmethod
    def _extract_sid(line: str) -> Optional[str]:
        match = SID_PATTERN.search(line or "")
        return match.group(1) if match else None

    @staticmethod
    def _error_signature(line: str) -> str:
        match = ERROR_TYPE_PATTERN.search(line)
        if match:
            return match.group(1)

        normalized = line.lower()
        if "timeout" in normalized:
            return "Timeout"
        if "503" in normalized or "upstream" in normalized:
            return "Gateway503"
        if "traceback" in normalized:
            return "Traceback"
        if "failed" in normalized:
            return "Failed"
        if "error" in normalized:
            return "Error"
        return "Unknown"

    @staticmethod
    def _root_cause_hint(error_hits: List[Dict[str, Any]], request_status: str) -> str:
        if not error_hits:
            if request_status == "success":
                return "no_error_detected"
            return "no_loki_error_log_found"

        lines = " ".join(hit.get("line", "").lower() for hit in error_hits)
        if "timeout" in lines:
            return "dependency_timeout"
        if "503" in lines or "upstream" in lines or "gateway" in lines:
            return "gateway_failure"
        if "traceback" in lines or "exception" in lines:
            return "application_exception"
        if "failed" in lines or "error" in lines:
            return "application_or_dependency_failure"
        return "unknown_failure"

    @staticmethod
    def _normalize_page_size(page_size: Optional[int]) -> int:
        if not page_size:
            return DEFAULT_PAGE_SIZE
        return max(1, min(int(page_size), MAX_PAGE_SIZE))

    @classmethod
    def _paginate(cls, items: List[Dict[str, Any]], page: int, page_size: int) -> Dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_page_size = cls._normalize_page_size(page_size)
        total = len(items)
        offset = (safe_page - 1) * safe_page_size
        return {
            "items": items[offset: offset + safe_page_size],
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
        }

    @staticmethod
    def _build_selector(container: str, levels: List[str]) -> str:
        selector = f'{{container="{container}"}}'
        label_levels = sorted({level.lower() for level in levels if level != "UNKNOWN"})
        if label_levels:
            level_pattern = "|".join(label_levels)
            selector = f'{{container="{container}", detected_level=~"(?i)({level_pattern})"}}'
        return selector

    @staticmethod
    def _prepare_logs(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        prepared_rows: List[Dict[str, Any]] = []
        for row in rows:
            raw_line = str(row.get("line") or "")
            stream = row.get("stream") if isinstance(row.get("stream"), dict) else {}
            parsed = parse_log_context(raw_line, stream=stream)
            resolved = resolve_correlation_id(parsed)

            prepared_rows.append(
                {
                    **row,
                    "raw_line": raw_line,
                    "line": parsed["clean_line"],
                    "rid": parsed["rid"],
                    "task_id": parsed.get("task_id"),
                    "request_id": parsed.get("request_id"),
                    "req_id": parsed.get("req_id"),
                    "sid": parsed["sid"],
                    "aid": parsed["aid"],
                    "parid": parsed["parid"],
                    "level": parsed["level"],
                    "correlation_id": resolved.get("id_value"),
                    "correlation_id_source": resolved.get("id_source"),
                }
            )

        prepared_rows.sort(key=lambda item: int(item.get("ts_ns") or 0))
        return prepared_rows

    @staticmethod
    def _filter_logs(logs: List[Dict[str, Any]], levels: List[str]) -> List[Dict[str, Any]]:
        if not levels:
            return logs
        accepted = set(levels)
        return [row for row in logs if row.get("level") in accepted]

    @staticmethod
    def _select_main_chain_aids(trace_chain: Dict[str, Any]) -> List[str]:
        nodes = trace_chain.get("nodes") if isinstance(trace_chain, dict) else []
        edges = trace_chain.get("edges") if isinstance(trace_chain, dict) else []
        root_nodes = trace_chain.get("root_nodes") if isinstance(trace_chain, dict) else []
        if not isinstance(nodes, list) or not nodes:
            return []

        node_map: Dict[str, Dict[str, Any]] = {
            str(node.get("aid")): node
            for node in nodes
            if isinstance(node, dict) and node.get("aid")
        }
        if not node_map:
            return []

        def _node_weight(aid: str) -> int:
            node = node_map.get(aid) or {}
            return int(node.get("log_count") or 0)

        candidate_roots = [aid for aid in root_nodes if aid in node_map]
        if not candidate_roots:
            candidate_roots = list(node_map.keys())
        start = sorted(candidate_roots, key=lambda aid: (-_node_weight(aid), aid))[0]

        children_map: Dict[str, List[tuple[str, int]]] = defaultdict(list)
        for edge in edges if isinstance(edges, list) else []:
            if not isinstance(edge, dict):
                continue
            parent = str(edge.get("from") or "").strip()
            child = str(edge.get("to") or "").strip()
            count = int(edge.get("count") or 0)
            if parent and child and parent in node_map and child in node_map:
                children_map[parent].append((child, count))

        main_chain: List[str] = []
        visited = set()
        current = start
        while current and current not in visited:
            main_chain.append(current)
            visited.add(current)
            children = children_map.get(current, [])
            if not children:
                break
            children = sorted(children, key=lambda item: (-item[1], -_node_weight(item[0]), item[0]))
            next_child = None
            for child, _ in children:
                if child not in visited:
                    next_child = child
                    break
            if not next_child:
                break
            current = next_child

        return main_chain

    @staticmethod
    def _mark_main_chain_logs(
        logs: List[Dict[str, Any]],
        request_id: str,
        session_id: str,
        main_chain_aids: List[str],
    ) -> List[Dict[str, Any]]:
        main_aid_set = set(main_chain_aids)
        marked: List[Dict[str, Any]] = []
        for row in logs:
            aid = str(row.get("aid") or "").strip()
            resolved_id = str(row.get("correlation_id") or "").strip()
            sid = str(row.get("sid") or "").strip()
            level = str(row.get("level") or "UNKNOWN")
            is_main_chain = bool(
                (aid and aid in main_aid_set)
                or (
                    not aid
                    and resolved_id == request_id
                    and (not session_id or sid == session_id)
                )
            )
            marked.append(
                {
                    **row,
                    "is_main_chain": is_main_chain,
                    "is_alert": level in ALERT_LEVELS,
                }
            )
        return marked

    @staticmethod
    def _build_main_chain_alerts(
        logs: List[Dict[str, Any]],
        request_id: str,
        session_id: str,
        main_chain_aids: List[str],
    ) -> Dict[str, Any]:
        marked = CorrelationService._mark_main_chain_logs(
            logs=logs,
            request_id=request_id,
            session_id=session_id,
            main_chain_aids=main_chain_aids,
        )
        alerts = [
            row
            for row in marked
            if row.get("is_main_chain") and str(row.get("level") or "UNKNOWN") in ALERT_LEVELS
        ]
        alerts.sort(key=lambda item: int(item.get("ts_ns") or 0))
        breakdown = Counter(str(row.get("level") or "UNKNOWN") for row in alerts)
        return {
            "main_chain_aids": main_chain_aids,
            "alert_count": len(alerts),
            "level_breakdown": dict(breakdown),
            "alert_logs": alerts[:100],
            "all_marked_logs": marked,
        }

    def _collect_request_ids_for_scope(
        self,
        aligned: AlignedRange,
        staff_code: Optional[str],
        session_id: Optional[str],
        request_id: Optional[str],
    ) -> set[str]:
        match: Dict[str, Any] = {
            "start_ts": {
                "$gte": aligned.start_utc,
                "$lte": aligned.end_utc,
            }
        }
        if staff_code:
            match["staff_code"] = staff_code
        if session_id:
            match["session_id"] = session_id
        if request_id:
            match["request_id"] = request_id

        ids = self.request_collection.distinct("request_id", match)
        return {str(item) for item in ids if item}

    @staticmethod
    def _update_time_range(node: Dict[str, Any], ts_utc: Optional[str]) -> None:
        if not ts_utc:
            return
        if not node.get("first_ts_utc") or ts_utc < node["first_ts_utc"]:
            node["first_ts_utc"] = ts_utc
        if not node.get("last_ts_utc") or ts_utc > node["last_ts_utc"]:
            node["last_ts_utc"] = ts_utc

    def _build_trace_chain(
        self,
        request_id: str,
        logs: List[Dict[str, Any]],
        request_doc: Dict[str, Any],
        agent_events: List[Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        nodes_map: Dict[str, Dict[str, Any]] = {}
        edges_counter: Counter = Counter()

        agent_by_aid: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "agent_codes": set(), "agent_names": set()}
        )
        for event in agent_events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            aid = str(payload.get("agent_id") or "").strip()
            if not aid:
                continue
            entry = agent_by_aid[aid]
            entry["count"] += 1
            if event.get("agent_code"):
                entry["agent_codes"].add(str(event.get("agent_code")))
            if event.get("agent_name"):
                entry["agent_names"].add(str(event.get("agent_name")))

        tool_by_aid: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "agent_codes": set(), "agent_names": set(), "tools": set()}
        )
        for row in tool_calls:
            aid = str(row.get("agent_id") or "").strip()
            if not aid:
                continue
            entry = tool_by_aid[aid]
            entry["count"] += 1
            if row.get("agent_code"):
                entry["agent_codes"].add(str(row.get("agent_code")))
            if row.get("agent_name"):
                entry["agent_names"].add(str(row.get("agent_name")))
            if row.get("tool"):
                entry["tools"].add(str(row.get("tool")))

        for row in logs:
            aid = str(row.get("aid") or "").strip()
            if not aid:
                continue

            parid = str(row.get("parid") or "-").strip() or "-"
            level = str(row.get("level") or "UNKNOWN")
            sid = str(row.get("sid") or "").strip()
            ts_utc = row.get("ts_utc")

            node = nodes_map.setdefault(
                aid,
                {
                    "aid": aid,
                    "parid": "-",
                    "_parent_candidates": set(),
                    "first_ts_utc": None,
                    "last_ts_utc": None,
                    "log_count": 0,
                    "level_breakdown": Counter(),
                    "sid_set": set(),
                    "mongo_agent_codes": set(),
                    "mongo_agent_names": set(),
                    "mongo_tools": set(),
                    "source_hits": {
                        "logs": 0,
                        "agent_payload": 0,
                        "tool_call": 0,
                    },
                },
            )

            node["log_count"] += 1
            node["source_hits"]["logs"] += 1
            node["level_breakdown"][level] += 1
            if sid:
                node["sid_set"].add(sid)
            self._update_time_range(node, ts_utc)

            if parid and parid != "-":
                node["_parent_candidates"].add(parid)
                edges_counter[(parid, aid)] += 1

        for aid, node in nodes_map.items():
            parents = sorted(node["_parent_candidates"])
            node["parid"] = parents[0] if parents else "-"

            agent_link = agent_by_aid.get(aid)
            if agent_link:
                node["source_hits"]["agent_payload"] = int(agent_link["count"])
                node["mongo_agent_codes"].update(agent_link["agent_codes"])
                node["mongo_agent_names"].update(agent_link["agent_names"])

            tool_link = tool_by_aid.get(aid)
            if tool_link:
                node["source_hits"]["tool_call"] = int(tool_link["count"])
                node["mongo_agent_codes"].update(tool_link["agent_codes"])
                node["mongo_agent_names"].update(tool_link["agent_names"])
                node["mongo_tools"].update(tool_link["tools"])

        unresolved_parent_set = {
            node["parid"]
            for node in nodes_map.values()
            if node.get("parid") and node.get("parid") != "-" and node["parid"] not in nodes_map
        }

        nodes: List[Dict[str, Any]] = []
        for node in nodes_map.values():
            nodes.append(
                {
                    "aid": node["aid"],
                    "parid": node["parid"],
                    "first_ts_utc": node["first_ts_utc"],
                    "last_ts_utc": node["last_ts_utc"],
                    "log_count": node["log_count"],
                    "level_breakdown": dict(node["level_breakdown"]),
                    "sid_candidates": sorted(node["sid_set"]),
                    "mongo_agent_codes": sorted(node["mongo_agent_codes"]),
                    "mongo_agent_names": sorted(node["mongo_agent_names"]),
                    "mongo_tools": sorted(node["mongo_tools"]),
                    "source_hits": node["source_hits"],
                }
            )
        nodes.sort(key=lambda item: (item.get("first_ts_utc") or "", item.get("aid") or ""))

        edges = [
            {"from": parent, "to": child, "count": int(count)}
            for (parent, child), count in edges_counter.items()
            if parent and parent != "-"
        ]
        edges.sort(key=lambda item: (-item["count"], item["from"], item["to"]))

        root_nodes = sorted(
            [
                node["aid"]
                for node in nodes
                if not node.get("parid") or node.get("parid") == "-" or node.get("parid") not in nodes_map
            ]
        )

        session_candidates = sorted(
            {
                sid
                for node in nodes
                for sid in node.get("sid_candidates", [])
                if sid
            }
        )
        session_id = str(request_doc.get("session_id") or "") or (session_candidates[0] if session_candidates else "")

        mongo_link_stats = {
            "aid_total": len(nodes),
            "aid_matched_in_agent_payload": sum(1 for node in nodes if node["source_hits"].get("agent_payload", 0) > 0),
            "aid_matched_in_tool_calls": sum(1 for node in nodes if node["source_hits"].get("tool_call", 0) > 0),
            "aid_matched_in_any_mongo": sum(
                1
                for node in nodes
                if node["source_hits"].get("agent_payload", 0) > 0 or node["source_hits"].get("tool_call", 0) > 0
            ),
            "unmatched_aids": sorted(
                [
                    node["aid"]
                    for node in nodes
                    if node["source_hits"].get("agent_payload", 0) == 0 and node["source_hits"].get("tool_call", 0) == 0
                ]
            ),
        }

        return {
            "request_id": request_id,
            "session_id": session_id,
            "nodes": nodes,
            "edges": edges,
            "root_nodes": root_nodes,
            "unresolved_parents": sorted(unresolved_parent_set),
            "mongo_link_stats": mongo_link_stats,
        }

    def time_align(self, start_local: str, end_local: str, tz: Optional[str], buffer_seconds: int) -> Dict[str, Any]:
        aligned = self.time_align_service.align_range(
            start_local=start_local,
            end_local=end_local,
            tz_name=tz,
            buffer_seconds=buffer_seconds,
        )
        return aligned.to_payload()

    def _loki_logs_by_rid(
        self,
        container: str,
        request_id: str,
        start_ns: int,
        end_ns: int,
        levels: Optional[List[str]] = None,
        limit: int = 2500,
    ) -> List[Dict[str, Any]]:
        normalized_levels = normalize_levels(levels)
        selector = self._build_selector(container, normalized_levels)
        # Use request_id literal instead of `rid=<id>` because some logs wrap values with ANSI codes.
        query = f'{selector} |= "{request_id}"'
        rows = self.loki_query_service.query_range(
            query=query,
            start_ns=start_ns,
            end_ns=end_ns,
            limit=limit,
            direction="forward",
        )
        prepared = self._prepare_logs(rows)

        result = []
        for row in prepared:
            resolved_id = row.get("correlation_id")
            if resolved_id and resolved_id != request_id:
                continue
            if not resolved_id and request_id not in str(row.get("line") or ""):
                continue
            result.append(row)

        return self._filter_logs(result, normalized_levels)

    def get_rid_correlation(
        self,
        request_id: str,
        container: str,
        window_sec: int = 120,
        levels: Optional[List[str]] = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        normalized_container = self._assert_container(container)
        normalized_levels = normalize_levels(levels)
        safe_page_size = self._normalize_page_size(page_size)

        request_doc = self.request_collection.find_one(
            {"request_id": request_id},
            {
                "_id": 0,
                "request_id": 1,
                "state_id": 1,
                "session_id": 1,
                "staff_code": 1,
                "status": 1,
                "query": 1,
                "error": 1,
                "start_ts": 1,
                "end_ts": 1,
                "duration_s": 1,
                "scene_result": 1,
                "token_usage_total": 1,
            },
        )
        if not request_doc:
            raise KeyError(f"request_id={request_id} not found")

        agent_events = list(
            self.agent_collection.find(
                {"request_id": request_id},
                {"_id": 0},
            ).sort("ts", 1)
        )
        tool_calls = list(
            self.tool_collection.find(
                {"request_id": request_id},
                {"_id": 0},
            ).sort("ts", 1)
        )

        timeline = self._build_timeline(agent_events)

        ts_candidates = [
            self._dt(request_doc.get("start_ts")),
            self._dt(request_doc.get("end_ts")),
            *(self._dt(item.get("ts")) for item in agent_events),
            *(self._dt(item.get("ts")) for item in tool_calls),
        ]
        ts_values = [item for item in ts_candidates if item is not None]
        if not ts_values:
            raise ValueError("request has no valid timestamps for correlation")

        start_utc = min(ts_values)
        end_utc = max(ts_values)
        aligned = self.time_align_service.align_range(
            start_local=start_utc.isoformat(),
            end_local=end_utc.isoformat(),
            tz_name="UTC",
            buffer_seconds=max(window_sec, 0),
        )

        logs = self._loki_logs_by_rid(
            container=normalized_container,
            request_id=request_id,
            start_ns=aligned.buffered_start_ns,
            end_ns=aligned.buffered_end_ns,
            levels=normalized_levels,
        )

        error_hits = [
            row
            for row in logs
            if row.get("level") == "ERROR"
            or any(keyword in str(row.get("line") or "").lower() for keyword in ERROR_KEYWORDS)
        ]
        hit_keywords = sorted(
            {
                keyword
                for row in error_hits
                for keyword in ERROR_KEYWORDS
                if keyword in str(row.get("line") or "").lower()
            }
        )

        level_breakdown = Counter(str(row.get("level") or "UNKNOWN") for row in logs)
        correlation_source_breakdown = Counter(str(row.get("correlation_id_source") or "unknown") for row in logs)

        session_id = str(request_doc.get("session_id") or "")
        rid_matches = sum(1 for row in logs if row.get("correlation_id") == request_id)
        sid_matches = sum(1 for row in logs if session_id and row.get("sid") == session_id)

        aids = {str(row.get("aid")) for row in logs if row.get("aid")}
        parids = {
            str(row.get("parid"))
            for row in logs
            if row.get("parid") and str(row.get("parid")) != "-"
        }

        request_status = str(request_doc.get("status", "")).lower()
        request_payload = {
            **request_doc,
            "duration_s": to_float(request_doc.get("duration_s"), 0.0),
            "token_total": self._token_total(request_doc),
        }

        trace_chain = self._build_trace_chain(
            request_id=request_id,
            logs=logs,
            request_doc=request_doc,
            agent_events=agent_events,
            tool_calls=tool_calls,
        )
        main_chain_aids = self._select_main_chain_aids(trace_chain)
        main_chain_highlights = self._build_main_chain_alerts(
            logs=logs,
            request_id=request_id,
            session_id=session_id,
            main_chain_aids=main_chain_aids,
        )
        marked_logs = main_chain_highlights.pop("all_marked_logs")
        logs_page = self._paginate(marked_logs, page=page, page_size=safe_page_size)

        return {
            "container": normalized_container,
            "request_id": request_id,
            "time_window": aligned.to_payload(),
            "request": request_payload,
            "agent_timeline": timeline,
            "agent_events": agent_events,
            "tool_calls": tool_calls,
            "loki_logs": logs_page["items"],
            "logs_page": logs_page,
            "trace_chain": trace_chain,
            "main_chain_highlights": main_chain_highlights,
            "log_summary": {
                "total_logs": len(logs),
                "error_hits": len(error_hits),
                "matched_keywords": hit_keywords,
                "level_breakdown": dict(level_breakdown),
            },
            "correlation_checks": {
                "rid_match_count": rid_matches,
                "sid_match_count": sid_matches,
                "request_session_id": session_id,
                "request_state_id": request_doc.get("state_id"),
                "aid_match_count": len(aids),
                "parid_match_count": len(parids),
                "id_source_breakdown": dict(correlation_source_breakdown),
            },
            "root_cause_hint": self._root_cause_hint(error_hits, request_status),
        }

    @staticmethod
    def _is_alert_row(row: Dict[str, Any]) -> bool:
        level = str(row.get("level") or "UNKNOWN")
        if level in ALERT_LEVELS:
            return True
        line = str(row.get("line") or "").lower()
        return any(keyword in line for keyword in ERROR_KEYWORDS)

    def _query_logs_by_request(
        self,
        container: str,
        request_id: str,
        start_ns: int,
        end_ns: int,
        levels: List[str],
        limit: int = 3000,
    ) -> List[Dict[str, Any]]:
        selector = self._build_selector(container, levels)
        query = f'{selector} |= "{request_id}"'
        rows = self.loki_query_service.query_range(
            query=query,
            start_ns=start_ns,
            end_ns=end_ns,
            limit=limit,
            direction="forward",
        )
        prepared = self._prepare_logs(rows)
        filtered = self._filter_logs(prepared, levels)
        return [
            row
            for row in filtered
            if row.get("correlation_id") == request_id
            or request_id in str(row.get("line") or "")
        ]

    @staticmethod
    def _resolve_correlation_id(logs: List[Dict[str, Any]], request_id: str) -> Dict[str, Any]:
        source_counts = Counter()
        for row in logs:
            if row.get("correlation_id") != request_id:
                continue
            source = str(row.get("correlation_id_source") or "unknown")
            source_counts[source] += 1

        for preferred in ("rid", "task_id", "request_id", "req_id"):
            if source_counts.get(preferred):
                return {
                    "resolved_value": request_id,
                    "resolved_by": preferred,
                    "source_hit_counts": dict(source_counts),
                }

        return {
            "resolved_value": request_id,
            "resolved_by": "literal_match",
            "source_hit_counts": dict(source_counts),
        }

    def _build_error_summary(
        self,
        main_logs: List[Dict[str, Any]],
        cbb_logs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        merged = [
            *[{**row, "channel": "main"} for row in main_logs],
            *[{**row, "channel": "cbb"} for row in cbb_logs],
        ]
        alert_logs = [row for row in merged if self._is_alert_row(row)]
        alert_logs.sort(key=lambda item: int(item.get("ts_ns") or 0))

        level_breakdown = Counter(str(row.get("level") or "UNKNOWN") for row in alert_logs)
        channel_breakdown = Counter(str(row.get("channel") or "unknown") for row in alert_logs)
        signature_breakdown = Counter(
            self._error_signature(str(row.get("line") or ""))
            for row in alert_logs
        )
        keywords = sorted(
            {
                keyword
                for row in alert_logs
                for keyword in ERROR_KEYWORDS
                if keyword in str(row.get("line") or "").lower()
            }
        )

        first_ts = alert_logs[0].get("ts_utc") if alert_logs else None
        last_ts = alert_logs[-1].get("ts_utc") if alert_logs else None

        return {
            "alert_count": len(alert_logs),
            "level_breakdown": dict(level_breakdown),
            "channel_breakdown": dict(channel_breakdown),
            "signature_breakdown": dict(signature_breakdown.most_common(10)),
            "matched_keywords": keywords,
            "first_alert_ts_utc": first_ts,
            "last_alert_ts_utc": last_ts,
        }

    def get_tool_call_correlation(
        self,
        request_id: str,
        container: str,
        tool: str,
        tool_id: Optional[str] = None,
        step: Optional[int] = None,
        levels: Optional[List[str]] = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        window_sec: int = 120,
    ) -> Dict[str, Any]:
        normalized_container = self._assert_container(container)
        effective_tool = enforce_container_tool(normalized_container, tool)
        if not effective_tool:
            raise ValueError("tool is required")

        cbb_container = normalized_container
        inferred_cbb_container = infer_cbb_container_by_tool(effective_tool, normalized_container)
        if inferred_cbb_container:
            cbb_container = inferred_cbb_container

        normalized_levels = normalize_levels(levels)
        safe_page_size = self._normalize_page_size(page_size)

        request_doc = self.request_collection.find_one(
            {"request_id": request_id},
            {
                "_id": 0,
                "request_id": 1,
                "state_id": 1,
                "session_id": 1,
                "status": 1,
                "query": 1,
                "start_ts": 1,
                "end_ts": 1,
                "error": 1,
            },
        ) or {}

        tool_match: Dict[str, Any] = {
            "request_id": request_id,
            "tool": effective_tool,
        }
        if tool_id:
            tool_match["tool_id"] = tool_id
        if step is not None:
            tool_match["step"] = step

        tool_cursor = self.tool_collection.find(tool_match, {"_id": 0}).sort("ts", 1)
        tool_rows = list(tool_cursor)
        selected_tool_call = tool_rows[-1] if tool_rows else None

        ts_candidates = [
            self._dt(request_doc.get("start_ts")),
            self._dt(request_doc.get("end_ts")),
            *(self._dt(item.get("ts")) for item in tool_rows),
        ]
        ts_values = [item for item in ts_candidates if item is not None]
        if ts_values:
            start_utc = min(ts_values)
            end_utc = max(ts_values)
        else:
            now_utc = datetime.now(timezone.utc)
            start_utc = now_utc
            end_utc = now_utc

        aligned = self.time_align_service.align_range(
            start_local=start_utc.isoformat(),
            end_local=end_utc.isoformat(),
            tz_name="UTC",
            buffer_seconds=max(window_sec, 0),
        )

        cbb_logs = self._query_logs_by_request(
            container=cbb_container,
            request_id=request_id,
            start_ns=aligned.buffered_start_ns,
            end_ns=aligned.buffered_end_ns,
            levels=normalized_levels,
        )
        main_container = infer_main_flow_container(normalized_container)
        main_logs = self._query_logs_by_request(
            container=main_container,
            request_id=request_id,
            start_ns=aligned.buffered_start_ns,
            end_ns=aligned.buffered_end_ns,
            levels=normalized_levels,
        )

        id_resolution = self._resolve_correlation_id(cbb_logs, request_id=request_id)
        error_summary = self._build_error_summary(main_logs=main_logs, cbb_logs=cbb_logs)

        return {
            "request_id": request_id,
            "container": cbb_container,
            "requested_container": normalized_container,
            "main_flow_container": main_container,
            "tool": effective_tool,
            "time_window": aligned.to_payload(),
            "request": request_doc,
            "tool_call": selected_tool_call
            or {
                "request_id": request_id,
                "tool": effective_tool,
                "status": "mongo_missing",
            },
            "tool_call_candidates": tool_rows,
            "id_resolution": id_resolution,
            "error_summary": error_summary,
            "main_flow_logs_page": self._paginate(main_logs, page=page, page_size=safe_page_size),
            "cbb_logs_page": self._paginate(cbb_logs, page=page, page_size=safe_page_size),
        }

    def get_error_clusters(
        self,
        container: str,
        start_local: str,
        end_local: str,
        tz: Optional[str],
        keywords: Optional[List[str]] = None,
        levels: Optional[List[str]] = None,
        staff_code: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        buffer_seconds: int = 120,
    ) -> Dict[str, Any]:
        normalized_container = self._assert_container(container)
        normalized_levels = normalize_levels(levels)
        safe_page_size = self._normalize_page_size(page_size)

        aligned = self.time_align_service.align_range(
            start_local=start_local,
            end_local=end_local,
            tz_name=tz,
            buffer_seconds=buffer_seconds,
        )

        keyword_terms = [term.strip().lower() for term in (keywords or []) if term and term.strip()]
        if not keyword_terms:
            keyword_terms = list(ERROR_KEYWORDS)

        scoped_request_ids = self._collect_request_ids_for_scope(
            aligned=aligned,
            staff_code=staff_code,
            session_id=session_id,
            request_id=request_id,
        )
        if request_id and request_id not in scoped_request_ids:
            empty_page = self._paginate([], page=page, page_size=safe_page_size)
            return {
                "container": normalized_container,
                "time_window": aligned.to_payload(),
                "keywords": keyword_terms,
                "levels": normalized_levels,
                "total_logs": 0,
                "clusters": [],
                "clusters_page": empty_page,
            }

        selector = self._build_selector(normalized_container, normalized_levels)
        keyword_pattern = "|".join(re.escape(term) for term in keyword_terms)

        query = f'{selector} |~ "(?i)({keyword_pattern})"'
        if request_id:
            query = f'{selector} |= "{request_id}" |~ "(?i)({keyword_pattern})"'
        elif scoped_request_ids and len(scoped_request_ids) <= 30:
            rid_pattern = "|".join(re.escape(item) for item in sorted(scoped_request_ids))
            query = f'{selector} |~ "({rid_pattern})" |~ "(?i)({keyword_pattern})"'

        logs = self.loki_query_service.query_range(
            query=query,
            start_ns=aligned.buffered_start_ns,
            end_ns=aligned.buffered_end_ns,
            limit=3000,
            direction="forward",
        )

        prepared_logs = self._prepare_logs(logs)
        filtered_logs = self._filter_logs(prepared_logs, normalized_levels)

        if scoped_request_ids:
            filtered_logs = [
                row
                for row in filtered_logs
                if row.get("correlation_id") and str(row.get("correlation_id")) in scoped_request_ids
            ]
        if request_id:
            filtered_logs = [row for row in filtered_logs if row.get("correlation_id") == request_id]
        if session_id:
            filtered_logs = [row for row in filtered_logs if row.get("sid") == session_id]

        grouped: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "error_type": "",
                "count": 0,
                "first_ts_utc": None,
                "last_ts_utc": None,
                "sample_request_ids": set(),
                "sample_lines": [],
                "level_breakdown": Counter(),
            }
        )

        for row in filtered_logs:
            line = str(row.get("line") or "")
            signature = self._error_signature(line)
            ts_utc = row.get("ts_utc")
            level = str(row.get("level") or "UNKNOWN")
            group = grouped[signature]

            group["error_type"] = signature
            group["count"] += 1
            group["level_breakdown"][level] += 1
            if group["first_ts_utc"] is None or (ts_utc and ts_utc < group["first_ts_utc"]):
                group["first_ts_utc"] = ts_utc
            if group["last_ts_utc"] is None or (ts_utc and ts_utc > group["last_ts_utc"]):
                group["last_ts_utc"] = ts_utc

            rid = row.get("correlation_id")
            if rid and len(group["sample_request_ids"]) < 5:
                group["sample_request_ids"].add(str(rid))
            if len(group["sample_lines"]) < 3:
                group["sample_lines"].append(line)

        clusters = [
            {
                "error_type": item["error_type"],
                "count": item["count"],
                "first_ts_utc": item["first_ts_utc"],
                "last_ts_utc": item["last_ts_utc"],
                "sample_request_ids": sorted(item["sample_request_ids"]),
                "sample_lines": item["sample_lines"],
                "level_breakdown": dict(item["level_breakdown"]),
            }
            for item in grouped.values()
        ]
        clusters.sort(key=lambda row: row["count"], reverse=True)

        clusters_page = self._paginate(clusters, page=page, page_size=safe_page_size)

        return {
            "container": normalized_container,
            "time_window": aligned.to_payload(),
            "keywords": keyword_terms,
            "levels": normalized_levels,
            "total_logs": len(filtered_logs),
            "clusters": clusters_page["items"],
            "clusters_page": clusters_page,
        }
