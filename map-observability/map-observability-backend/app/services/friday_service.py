from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.services.analytics_service import AnalyticsService
from app.services.container_mapping import (
    MAIN_FLOW_CONTAINERS,
    assert_container_supported,
)
from app.services.correlation_service import CorrelationService
from app.services.filters import FilterOptions

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SLOW_THRESHOLD_S = 5.0
DEFAULT_STAGE_TIMEOUT_S = 20
ERROR_KEYWORDS = ["error", "exception", "failed", "traceback", "timeout"]
RID_IN_LINE_PATTERN = re.compile(r"(?:rid|request_id|req_id|task_id)\s*[:=]\s*([A-Za-z0-9_-]{8,128})", re.IGNORECASE)


class FridayService:
    def __init__(
        self,
        settings: Settings,
        analytics_service: AnalyticsService,
        correlation_service: CorrelationService,
        completion_client: Optional[Callable[[str, str, List[Dict[str, str]]], str]] = None,
    ) -> None:
        self.settings = settings
        self.analytics_service = analytics_service
        self.correlation_service = correlation_service
        self.completion_client = completion_client or self._call_openai_compatible
        self.report_collection = analytics_service.database["friday_reports"]
        self.report_config_collection = analytics_service.database["friday_report_config"]
        self._scheduler_task: asyncio.Task | None = None
        self._scheduler_stop: asyncio.Event | None = None

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        value = str(raw or "").strip()
        if not value:
            raise ValueError("base_url 不能为空")
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @staticmethod
    def _normalize_model(raw: str) -> str:
        value = str(raw or "").strip()
        if not value:
            raise ValueError("model 不能为空")
        return value

    @property
    def _config_file(self) -> Path:
        return Path(self.settings.friday_model_env_file).expanduser()

    @staticmethod
    def _read_env_file(path: Path) -> Dict[str, str]:
        if not path.exists():
            return {}

        result: Dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    @staticmethod
    def _write_env_file(path: Path, values: Dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = [
            "# Friday model config (saved by Settings UI)",
            f"FRIDAY_MODEL_BASE_URL={values.get('FRIDAY_MODEL_BASE_URL', '')}",
            f"FRIDAY_MODEL_NAME={values.get('FRIDAY_MODEL_NAME', '')}",
            "",
        ]
        path.write_text("\n".join(content), encoding="utf-8")

    def get_config(self) -> Dict[str, Any]:
        file_values = self._read_env_file(self._config_file)

        active_base_url = str(self.settings.friday_model_base_url or "").strip()
        active_model = str(self.settings.friday_model_name or "").strip()

        saved_base_url = str(file_values.get("FRIDAY_MODEL_BASE_URL") or "").strip()
        saved_model = str(file_values.get("FRIDAY_MODEL_NAME") or "").strip()

        display_base_url = saved_base_url or active_base_url
        display_model = saved_model or active_model

        restart_required = False
        if saved_base_url or saved_model:
            restart_required = (saved_base_url != active_base_url) or (saved_model != active_model)

        return {
            "configured": bool(display_base_url and display_model),
            "base_url": display_base_url,
            "model": display_model,
            "restart_required": restart_required,
            "active_base_url": active_base_url,
            "active_model": active_model,
            "config_file": str(self._config_file),
        }

    def update_config(self, base_url: str, model: str) -> Dict[str, Any]:
        normalized_base_url = self._normalize_base_url(base_url)
        normalized_model = self._normalize_model(model)

        self._write_env_file(
            self._config_file,
            {
                "FRIDAY_MODEL_BASE_URL": normalized_base_url,
                "FRIDAY_MODEL_NAME": normalized_model,
            },
        )

        payload = self.get_config()
        payload["restart_required"] = True
        return payload

    @staticmethod
    def _json_default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _build_sse(event: str, payload: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=FridayService._json_default)}\n\n"

    def get_report_config(self) -> Dict[str, Any]:
        existing = self.report_config_collection.find_one({"_id": "default"}, {"_id": 0})
        if existing:
            return existing
        return {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "weekly_day": 0,
            "weekly_hour": 9,
            "monthly_day": 1,
            "monthly_hour": 9,
            "monthly_minute": 15,
            "slow_threshold_s": DEFAULT_SLOW_THRESHOLD_S,
        }

    def update_report_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_report_config()
        updated = {**current, **payload, "updated_at": datetime.now(timezone.utc)}
        self.report_config_collection.update_one(
            {"_id": "default"},
            {"$set": updated},
            upsert=True,
        )
        updated.pop("_id", None)
        return updated

    def list_reports(self, report_type: str | None = None, limit: int = 20) -> Dict[str, Any]:
        match: Dict[str, Any] = {}
        if report_type:
            match["report_type"] = report_type
        rows = list(
            self.report_collection.find(
                match,
                {
                    "_id": 0,
                    "report_id": 1,
                    "report_type": 1,
                    "title": 1,
                    "period_start": 1,
                    "period_end": 1,
                    "generated_at": 1,
                    "created_at": 1,
                    "timezone": 1,
                    "status": 1,
                    "summary": 1,
                    "metrics": 1,
                },
            ).sort("created_at", -1).limit(max(1, min(limit, 100)))
        )
        return {"items": rows}

    def get_report(self, report_id: str) -> Dict[str, Any]:
        doc = self.report_collection.find_one({"report_id": report_id}, {"_id": 0})
        if not doc:
            raise KeyError(f"report_id={report_id} not found")
        return doc

    def run_report(self, report_type: str = "weekly", lookback_days: int = 7) -> Dict[str, Any]:
        return self._generate_report(report_type=report_type, lookback_days=lookback_days)

    def start_scheduler(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        self._scheduler_stop = asyncio.Event()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop_scheduler(self) -> None:
        if self._scheduler_stop is not None:
            self._scheduler_stop.set()
        if self._scheduler_task is not None:
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
        self._scheduler_task = None
        self._scheduler_stop = None

    async def _scheduler_loop(self) -> None:
        assert self._scheduler_stop is not None
        while not self._scheduler_stop.is_set():
            try:
                self._run_due_reports()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._scheduler_stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue

    def _run_due_reports(self) -> None:
        cfg = self.get_report_config()
        if not cfg.get("enabled", True):
            return
        tz = self._safe_zoneinfo(str(cfg.get("timezone") or "Asia/Shanghai"))
        now = datetime.now(tz)
        weekly_due = (
            now.weekday() == int(cfg.get("weekly_day", 0))
            and now.hour == int(cfg.get("weekly_hour", 9))
        )
        monthly_due = (
            now.day == int(cfg.get("monthly_day", 1))
            and now.hour == int(cfg.get("monthly_hour", 9))
            and now.minute >= int(cfg.get("monthly_minute", 15))
        )
        if weekly_due:
            self._generate_report_once_per_day("weekly", 7, now)
        if monthly_due:
            self._generate_report_once_per_day("monthly", 31, now)

    def _generate_report_once_per_day(self, report_type: str, lookback_days: int, now: datetime) -> None:
        key = f"{report_type}-{now.date().isoformat()}"
        if self.report_collection.find_one({"schedule_key": key}, {"_id": 1}):
            return
        self._generate_report(report_type=report_type, lookback_days=lookback_days, schedule_key=key)

    def _generate_report(
        self,
        *,
        report_type: str,
        lookback_days: int,
        schedule_key: str | None = None,
    ) -> Dict[str, Any]:
        cfg = self.get_report_config()
        tz = self._safe_zoneinfo(str(cfg.get("timezone") or "Asia/Shanghai"))
        period_end = datetime.now(tz)
        period_start = period_end - timedelta(days=lookback_days)
        start_utc = period_start.astimezone(timezone.utc)
        end_utc = period_end.astimezone(timezone.utc)
        slow_threshold_s = float(cfg.get("slow_threshold_s") or DEFAULT_SLOW_THRESHOLD_S)
        evidence = self._collect_report_evidence(start_utc, end_utc, slow_threshold_s)
        title = (
            f"MAP 调用质量周报（过去 {lookback_days} 天）"
            if report_type == "weekly"
            else f"MAP 调用质量月报（过去 {lookback_days} 天）"
        )
        markdown = self._build_report_markdown(title, evidence, slow_threshold_s)
        generated_at = datetime.now(timezone.utc)
        report = {
            "report_id": f"friday-{uuid4().hex[:12]}",
            "schedule_key": schedule_key,
            "report_type": report_type,
            "title": title,
            "period_start": period_start,
            "period_end": period_end,
            "generated_at": generated_at,
            "created_at": generated_at,
            "timezone": str(cfg.get("timezone") or "Asia/Shanghai"),
            "status": "success",
            "summary": evidence["summary"],
            "metrics": evidence["metrics"],
            "sections": evidence,
            "markdown": markdown,
        }
        self.report_collection.insert_one(report)
        report.pop("_id", None)
        return report

    def _collect_report_evidence(
        self,
        start_utc: datetime,
        end_utc: datetime,
        slow_threshold_s: float,
    ) -> Dict[str, Any]:
        request_match = {"start_ts": {"$gte": start_utc, "$lte": end_utc}}
        request_docs = list(
            self.analytics_service.request_collection.find(
                request_match,
                {
                    "_id": 0,
                    "request_id": 1,
                    "status": 1,
                    "error": 1,
                    "duration_s": 1,
                    "start_ts": 1,
                    "query": 1,
                },
            )
        )
        request_ids = [doc.get("request_id") for doc in request_docs if doc.get("request_id")]
        tool_docs = list(
            self.analytics_service.tool_collection.find(
                {"request_id": {"$in": request_ids}},
                {
                    "_id": 0,
                    "request_id": 1,
                    "agent_code": 1,
                    "tool": 1,
                    "status": 1,
                    "duration_s": 1,
                    "error": 1,
                    "output": 1,
                    "ts": 1,
                },
            )
        )
        llm_docs = list(
            self.analytics_service.llm_collection.find(
                {"request_id": {"$in": request_ids}},
                {
                    "_id": 0,
                    "request_id": 1,
                    "agent_code": 1,
                    "component": 1,
                    "phase": 1,
                    "model": 1,
                    "status": 1,
                    "duration_s": 1,
                    "error": 1,
                    "start_ts": 1,
                },
            )
        )

        failed_requests = [
            doc for doc in request_docs if str(doc.get("status")).lower() not in {"success", "ok"}
        ][:20]
        tool_failures = []
        data_failures = []
        for doc in tool_docs:
            output = doc.get("output") if isinstance(doc.get("output"), dict) else {}
            error = doc.get("error") or output.get("error")
            failed = str(doc.get("status")).lower() not in {"success", "ok", "none"} or bool(error)
            if not failed:
                continue
            item = {**doc, "reason": str(error or doc.get("status") or "tool_failed")}
            tool_failures.append(item)
            if str(doc.get("tool") or "").lower() in {
                "ask_database_agent",
                "wenshu_agent",
                "search_mounted_kb_agent",
                "query_kb_chunk_tool",
                "search_kb_chunk_tool",
            }:
                data_failures.append(item)

        slow_requests = [
            doc for doc in request_docs if float(doc.get("duration_s") or 0) >= slow_threshold_s
        ]
        slow_tools = [
            doc for doc in tool_docs if float(doc.get("duration_s") or 0) >= slow_threshold_s
        ]
        slow_llm = [
            doc for doc in llm_docs if float(doc.get("duration_s") or 0) >= slow_threshold_s
        ]
        llm_failures = [
            doc for doc in llm_docs if str(doc.get("status")).lower() != "success"
        ]

        error_clusters = self._cluster_errors(
            [doc.get("error") for doc in failed_requests]
            + [doc.get("reason") for doc in tool_failures]
            + [doc.get("error") for doc in llm_failures]
        )
        summary = {
            "request_count": len(request_docs),
            "failed_request_count": len(failed_requests),
            "tool_failure_count": len(tool_failures),
            "data_failure_count": len(data_failures),
            "llm_failure_count": len(llm_failures),
            "slow_request_count": len(slow_requests),
            "slow_tool_count": len(slow_tools),
            "slow_llm_count": len(slow_llm),
        }
        metrics = {
            **summary,
            "failure_rate": (len(failed_requests) / len(request_docs)) if request_docs else 0,
        }
        return {
            "summary": summary,
            "metrics": metrics,
            "tool_failures": tool_failures[:30],
            "data_failures": data_failures[:30],
            "llm_failures": llm_failures[:30],
            "slow_requests": sorted(slow_requests, key=lambda x: float(x.get("duration_s") or 0), reverse=True)[:30],
            "slow_tools": sorted(slow_tools, key=lambda x: float(x.get("duration_s") or 0), reverse=True)[:30],
            "slow_llm": sorted(slow_llm, key=lambda x: float(x.get("duration_s") or 0), reverse=True)[:30],
            "failed_requests": failed_requests,
            "error_clusters": error_clusters,
            "suggested_actions": self._suggest_report_actions(summary, error_clusters),
        }

    @staticmethod
    def _cluster_errors(errors: List[Any]) -> List[Dict[str, Any]]:
        clusters: Dict[str, Dict[str, Any]] = {}
        for error in errors:
            text = str(error or "").strip()
            if not text:
                continue
            key = text[:120].lower()
            item = clusters.setdefault(key, {"reason": text[:300], "count": 0})
            item["count"] += 1
        rows = list(clusters.values())
        rows.sort(key=lambda item: item["count"], reverse=True)
        return rows[:20]

    @staticmethod
    def _suggest_report_actions(summary: Dict[str, int], clusters: List[Dict[str, Any]]) -> List[str]:
        actions: List[str] = []
        if summary.get("tool_failure_count", 0) > 0:
            actions.append("优先查看失败次数最高的工具，补充超时、鉴权和入参校验日志。")
        if summary.get("data_failure_count", 0) > 0:
            actions.append("对知识库/问表/问数类数据源增加可用性探针和空结果区分。")
        if summary.get("slow_llm_count", 0) > 0:
            actions.append("按 phase 检查慢 LLM 调用，优先优化 Master 路由和 sub-agent 工具选择提示词。")
        if clusters:
            actions.append(f"聚类最高错误为：{clusters[0]['reason']}，建议作为本周首个排障主题。")
        return actions or ["本周期未发现显著失败或慢调用，可继续观察趋势。"]

    @staticmethod
    def _build_report_markdown(title: str, evidence: Dict[str, Any], slow_threshold_s: float) -> str:
        summary = evidence["summary"]
        lines = [
            f"# {title}",
            "",
            "## 概览",
            f"- 请求总数：{summary['request_count']}",
            f"- 失败请求：{summary['failed_request_count']}",
            f"- 工具失败：{summary['tool_failure_count']}，数据获取失败：{summary['data_failure_count']}",
            f"- 慢调用阈值：{slow_threshold_s}s；慢请求/工具/LLM：{summary['slow_request_count']}/{summary['slow_tool_count']}/{summary['slow_llm_count']}",
            "",
            "## 错误聚类",
        ]
        for item in evidence["error_clusters"][:8]:
            lines.append(f"- {item['count']} 次：{item['reason']}")
        if not evidence["error_clusters"]:
            lines.append("- 暂无")
        lines.extend(["", "## 建议动作"])
        for item in evidence["suggested_actions"]:
            lines.append(f"- {item}")
        return "\n".join(lines)

    @staticmethod
    def _split_text_chunks(text: str, chunk_size: int = 18) -> List[str]:
        raw = str(text or "")
        if not raw:
            return []
        return [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]

    def _resolve_model_config(self) -> Dict[str, str]:
        cfg = self.get_config()
        base_url = str(cfg.get("active_base_url") or "").strip()
        model = str(cfg.get("active_model") or "").strip()

        if not base_url or not model:
            raise RuntimeError("Friday 模型未配置或未生效，请先在设置中保存配置并重启 backend")

        return {"base_url": base_url, "model": model}

    @staticmethod
    def _is_error_intent(message: str) -> bool:
        lower = message.lower()
        keywords = ["error", "exception", "failed", "traceback", "timeout", "报错", "失败", "异常", "告警", "warning"]
        return any(item in lower for item in keywords)

    @staticmethod
    def _is_slow_intent(message: str) -> bool:
        lower = message.lower()
        keywords = ["slow", "latency", "慢", "耗时", "卡", "延迟"]
        return any(item in lower for item in keywords)

    def _detect_intent(self, message: str) -> str:
        is_slow = self._is_slow_intent(message)
        is_error = self._is_error_intent(message)
        if is_slow and is_error:
            return "mixed"
        if is_slow:
            return "slow"
        if is_error:
            return "error"
        return "mixed"

    @staticmethod
    def _extract_request_id(message: str, context_overrides: Optional[Dict[str, Any]]) -> Optional[str]:
        if context_overrides:
            for key in ("request_id", "rid"):
                value = str(context_overrides.get(key) or "").strip()
                if value:
                    return value

        match = RID_IN_LINE_PATTERN.search(message or "")
        if match:
            return match.group(1)

        return None

    def _make_filter(self, start_utc: datetime, end_utc: datetime, container: str) -> FilterOptions:
        return FilterOptions(
            start_ts=start_utc,
            end_ts=end_utc,
            container=container,
            status=None,
            staff_code=None,
            session_id=None,
            request_id=None,
            agent_code=None,
            tool=None,
            query_like=None,
        )

    def _safe_zoneinfo(self, tz_name: str) -> timezone:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return timezone.utc

    def _collect_slow_evidence(self, start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        for container in sorted(MAIN_FLOW_CONTAINERS):
            try:
                payload = self.analytics_service.list_requests(
                    filters=self._make_filter(start_utc, end_utc, container),
                    page=1,
                    page_size=10,
                    sort_by="duration_s",
                    sort_order="desc",
                )
            except Exception:
                continue
            for item in payload.get("items", []):
                duration = float(item.get("duration_s") or 0.0)
                if duration < DEFAULT_SLOW_THRESHOLD_S:
                    continue
                rows.append(
                    {
                        "container": container,
                        "request_id": item.get("request_id"),
                        "duration_s": duration,
                        "status": item.get("status"),
                        "staff_code": item.get("staff_code"),
                        "start_ts": item.get("start_ts"),
                    }
                )

        rows.sort(key=lambda item: float(item.get("duration_s") or 0.0), reverse=True)
        return rows[:10]

    def _collect_overview_evidence(self, start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
        if not hasattr(self.analytics_service, "get_overview"):
            return []

        rows: List[Dict[str, Any]] = []
        for container in sorted(MAIN_FLOW_CONTAINERS):
            try:
                payload = self.analytics_service.get_overview(
                    self._make_filter(start_utc, end_utc, container)
                )
            except Exception:
                continue

            rows.append(
                {
                    "container": container,
                    "total_requests": int(payload.get("total_requests") or 0),
                    "success_rate": float(payload.get("success_rate") or 0.0),
                    "error_rate": float(payload.get("error_rate") or 0.0),
                    "duration_avg_s": float(payload.get("duration_s", {}).get("avg") or 0.0),
                    "duration_p95_s": float(payload.get("duration_s", {}).get("p95") or 0.0),
                    "token_total": float(payload.get("token", {}).get("total") or 0.0),
                }
            )

        return rows

    def _collect_error_evidence(self, start_local: str, end_local: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        for container in sorted(MAIN_FLOW_CONTAINERS):
            try:
                payload = self.correlation_service.get_error_clusters(
                    container=container,
                    start_local=start_local,
                    end_local=end_local,
                    tz=self.settings.default_tz,
                    keywords=list(ERROR_KEYWORDS),
                    levels=["ERROR", "WARNING"],
                    staff_code=None,
                    session_id=None,
                    request_id=None,
                    page=1,
                    page_size=10,
                    buffer_seconds=120,
                )
            except Exception:
                continue
            for item in payload.get("clusters_page", {}).get("items", []):
                rows.append(
                    {
                        "container": container,
                        "error_type": item.get("error_type"),
                        "count": int(item.get("count") or 0),
                        "sample_request_ids": item.get("sample_request_ids") or [],
                        "first_ts_utc": item.get("first_ts_utc"),
                        "last_ts_utc": item.get("last_ts_utc"),
                    }
                )

        rows.sort(key=lambda item: int(item.get("count") or 0), reverse=True)
        return rows[:10]

    def _collect_request_trace_evidence(
        self,
        request_id: str,
        preferred_container: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        candidates: List[str] = []
        if preferred_container:
            try:
                normalized = assert_container_supported(preferred_container)
                candidates.append(normalized)
            except ValueError:
                pass
        for fallback in sorted(MAIN_FLOW_CONTAINERS):
            if fallback not in candidates:
                candidates.append(fallback)

        selected_payload: Optional[Dict[str, Any]] = None
        for container in candidates:
            try:
                payload = self.correlation_service.get_rid_correlation(
                    request_id=request_id,
                    container=container,
                    window_sec=120,
                    levels=["ERROR", "WARNING", "INFO", "DEBUG"],
                    page=1,
                    page_size=10,
                )
            except Exception:
                continue
            selected_payload = payload
            total_logs = int(payload.get("logs_page", {}).get("total") or 0)
            if total_logs > 0:
                break

        if not selected_payload:
            return None

        logs = selected_payload.get("logs_page", {}).get("items") or []
        return {
            "request_id": request_id,
            "container": selected_payload.get("container"),
            "root_cause_hint": selected_payload.get("root_cause_hint"),
            "error_hits": int(selected_payload.get("log_summary", {}).get("error_hits") or 0),
            "rid_match_count": int(selected_payload.get("correlation_checks", {}).get("rid_match_count") or 0),
            "sample_logs": [item.get("line") for item in logs[:3] if item.get("line")],
            "tool_calls_brief": [
                {
                    "tool": item.get("tool"),
                    "tool_id": item.get("tool_id"),
                    "status": item.get("status"),
                    "step": item.get("step"),
                    "duration_s": item.get("duration_s"),
                }
                for item in (selected_payload.get("tool_calls") or [])[:5]
            ],
        }

    def _collect_tool_call_evidence(self, trace_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not trace_row:
            return None
        if not hasattr(self.correlation_service, "get_tool_call_correlation"):
            return None

        request_id = str(trace_row.get("request_id") or "").strip()
        if not request_id:
            return None

        tool_calls = trace_row.get("tool_calls_brief") or []
        if not isinstance(tool_calls, list) or not tool_calls:
            return None

        selected = None
        for row in tool_calls:
            tool = str((row or {}).get("tool") or "").strip()
            if tool:
                selected = row
                break

        if not selected:
            return None

        tool = str(selected.get("tool") or "").strip()
        if not tool:
            return None

        container = str(trace_row.get("container") or "map_core-dev").strip()
        try:
            payload = self.correlation_service.get_tool_call_correlation(
                request_id=request_id,
                container=container,
                tool=tool,
                tool_id=selected.get("tool_id"),
                step=selected.get("step"),
                levels=["ERROR", "WARNING", "INFO", "DEBUG"],
                page=1,
                page_size=10,
                window_sec=120,
            )
        except Exception as exc:
            return {"request_id": request_id, "tool": tool, "error": str(exc)}

        return {
            "request_id": request_id,
            "tool": tool,
            "tool_id": selected.get("tool_id"),
            "status": payload.get("tool_call", {}).get("status"),
            "duration_s": payload.get("tool_call", {}).get("duration_s"),
            "main_flow_container": payload.get("main_flow_container"),
            "tool_container": payload.get("container"),
            "error_summary": payload.get("error_summary") or {},
            "sample_main_logs": [
                item.get("line")
                for item in (payload.get("main_flow_logs_page", {}).get("items") or [])[:3]
                if item.get("line")
            ],
            "sample_tool_logs": [
                item.get("line")
                for item in (payload.get("cbb_logs_page", {}).get("items") or [])[:3]
                if item.get("line")
            ],
        }

    def _build_actions(
        self,
        slow_rows: List[Dict[str, Any]],
        error_rows: List[Dict[str, Any]],
        trace_row: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []

        if trace_row and trace_row.get("request_id"):
            actions.append(
                {
                    "type": "open_request_detail",
                    "label": f"打开请求详情 {trace_row['request_id']}",
                    "request_id": trace_row["request_id"],
                }
            )
            actions.append(
                {
                    "type": "open_traces",
                    "label": f"在 Traces 追踪 {trace_row['request_id']}",
                    "request_id": trace_row["request_id"],
                    "container": trace_row.get("container"),
                }
            )

        if slow_rows and slow_rows[0].get("request_id"):
            actions.append(
                {
                    "type": "open_request_detail",
                    "label": f"打开最慢请求 {slow_rows[0]['request_id']}",
                    "request_id": slow_rows[0]["request_id"],
                }
            )

        if error_rows:
            actions.append(
                {
                    "type": "open_traces",
                    "label": f"查看错误最高容器 {error_rows[0].get('container', 'map_core-dev')}",
                    "container": error_rows[0].get("container", "map_core-dev"),
                }
            )

        unique: Dict[str, Dict[str, Any]] = {}
        for action in actions:
            key = f"{action.get('type')}::{action.get('request_id')}::{action.get('container')}"
            unique[key] = action
        return list(unique.values())[:4]

    def _build_model_messages(
        self,
        message: str,
        intent: str,
        evidence_payload: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        compact_evidence = {
            "scope": {
                "lookback_days": int(evidence_payload.get("scope", {}).get("lookback_days") or DEFAULT_LOOKBACK_DAYS),
                "slow_threshold_s": float(evidence_payload.get("scope", {}).get("slow_threshold_s") or DEFAULT_SLOW_THRESHOLD_S),
            },
            "intent": str(evidence_payload.get("intent") or intent),
            "request_id": evidence_payload.get("request_id"),
            "overview_snapshot": [
                {
                    "container": row.get("container"),
                    "total_requests": row.get("total_requests"),
                    "success_rate": row.get("success_rate"),
                    "error_rate": row.get("error_rate"),
                    "duration_p95_s": row.get("duration_p95_s"),
                }
                for row in (evidence_payload.get("overview_snapshot") or [])[:2]
            ],
            "slow_calls_top": [
                {
                    "request_id": row.get("request_id"),
                    "container": row.get("container"),
                    "duration_s": row.get("duration_s"),
                    "status": row.get("status"),
                }
                for row in (evidence_payload.get("slow_calls_top") or [])[:5]
            ],
            "error_clusters_top": [
                {
                    "container": row.get("container"),
                    "error_type": row.get("error_type"),
                    "count": row.get("count"),
                    "sample_request_ids": (row.get("sample_request_ids") or [])[:2],
                }
                for row in (evidence_payload.get("error_clusters_top") or [])[:5]
            ],
            "request_trace": evidence_payload.get("request_trace") or None,
            "tool_call_trace": evidence_payload.get("tool_call_trace") or None,
        }

        messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是 MAP 日志诊断助手 Friday。"
                    "请使用中文输出，结构固定为：结论、证据、建议。"
                    "结论要直接说明是否存在慢调用/错误链路；"
                    "证据要引用 request_id、container、日志现象；"
                    "建议要给可执行优化动作。"
                ),
            }
        ]

        for item in history[-10:]:
            role = str(item.get("role") or "user").lower()
            if role not in {"user", "assistant", "system"}:
                role = "user"
            content = str(item.get("content") or "").strip()
            if content:
                messages.append({"role": role, "content": content})

        messages.append(
            {
                "role": "user",
                "content": (
                    f"用户问题：{message}\n"
                    f"诊断意图：{intent}\n"
                    f"诊断证据(JSON)：{json.dumps(compact_evidence, ensure_ascii=False, default=self._json_default)}\n"
                    "请严格根据证据给出分析，不要编造未出现的日志。"
                ),
            }
        )
        return messages

    def _resolve_chat_endpoint(self, base_url: str) -> str:
        raw = base_url.rstrip("/")
        if raw.endswith("/chat/completions"):
            return raw
        if raw.endswith("/v1"):
            return f"{raw}/chat/completions"
        return f"{raw}/v1/chat/completions"

    def _stream_openai_compatible(self, base_url: str, model: str, messages: List[Dict[str, str]]) -> Iterator[str]:
        endpoint = self._resolve_chat_endpoint(base_url)
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 8192,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = Request(
            url=endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=max(30, int(self.settings.friday_model_timeout_s))) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}

                    delta = choice.get("delta")
                    if isinstance(delta, dict):
                        piece = delta.get("content")
                        if piece:
                            yield str(piece)
                            continue

                    message_obj = choice.get("message")
                    if isinstance(message_obj, dict):
                        piece = message_obj.get("content")
                        if piece:
                            yield str(piece)
                            continue

                    text_piece = choice.get("text")
                    if text_piece:
                        yield str(text_piece)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"模型流式调用失败: HTTP {exc.code} - {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"模型流式调用失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("模型流式调用超时") from exc

    def _call_openai_compatible(self, base_url: str, model: str, messages: List[Dict[str, str]]) -> str:
        endpoint = self._resolve_chat_endpoint(base_url)
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 512,
            "stream": False,
        }
        request = Request(
            url=endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=max(30, int(self.settings.friday_model_timeout_s))) as response:
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"模型调用失败: HTTP {exc.code} - {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"模型调用失败: {exc.reason}") from exc

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("模型返回非 JSON 响应") from exc

        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("模型返回内容为空")

        message_obj = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message_obj.get("content") if isinstance(message_obj, dict) else None
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
            content = "".join(text_parts)

        text = str(content or "").strip()
        if not text:
            raise RuntimeError("模型返回文本为空")
        return text

    async def stream_chat(self, payload: Dict[str, Any]) -> AsyncIterator[str]:
        conversation_id = str(payload.get("conversation_id") or uuid4().hex)
        message = str(payload.get("message") or "").strip()
        if not message:
            yield self._build_sse("error", {"message": "message 不能为空"})
            yield self._build_sse("done", {"conversation_id": conversation_id})
            return

        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        context_overrides = payload.get("context_overrides") if isinstance(payload.get("context_overrides"), dict) else {}

        now_utc = datetime.now(timezone.utc)
        start_utc = now_utc - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        tz_obj = self._safe_zoneinfo(self.settings.default_tz)
        start_local = start_utc.astimezone(tz_obj).isoformat()
        end_local = now_utc.astimezone(tz_obj).isoformat()

        intent = self._detect_intent(message)
        request_id = self._extract_request_id(message, context_overrides)

        stage = "init"
        try:
            # 先发出进度事件，避免前端在证据聚合阶段长时间无响应。
            yield self._build_sse(
                "progress",
                {"stage": stage, "message": "已接收问题，正在准备诊断上下文..."},
            )
            await asyncio.sleep(0)

            warnings: List[str] = []

            stage = "collect_slow"
            yield self._build_sse(
                "progress",
                {"stage": stage, "message": "正在收集慢调用证据..."},
            )
            await asyncio.sleep(0)
            try:
                slow_rows = await asyncio.wait_for(
                    asyncio.to_thread(self._collect_slow_evidence, start_utc, now_utc),
                    timeout=DEFAULT_STAGE_TIMEOUT_S,
                )
            except Exception as exc:
                slow_rows = []
                warnings.append(f"{stage}: {exc}")
                yield self._build_sse(
                    "progress",
                    {"stage": stage, "message": "慢调用证据收集超时或失败，已跳过。"},
                )
                await asyncio.sleep(0)

            stage = "collect_overview"
            yield self._build_sse(
                "progress",
                {"stage": stage, "message": "正在聚合全局概览指标..."},
            )
            await asyncio.sleep(0)
            try:
                overview_rows = await asyncio.wait_for(
                    asyncio.to_thread(self._collect_overview_evidence, start_utc, now_utc),
                    timeout=DEFAULT_STAGE_TIMEOUT_S,
                )
            except Exception as exc:
                overview_rows = []
                warnings.append(f"{stage}: {exc}")
                yield self._build_sse(
                    "progress",
                    {"stage": stage, "message": "概览聚合超时或失败，已跳过。"},
                )
                await asyncio.sleep(0)

            stage = "collect_errors"
            yield self._build_sse(
                "progress",
                {"stage": stage, "message": "正在聚类错误与告警日志..."},
            )
            await asyncio.sleep(0)
            try:
                error_rows = await asyncio.wait_for(
                    asyncio.to_thread(self._collect_error_evidence, start_local, end_local),
                    timeout=DEFAULT_STAGE_TIMEOUT_S,
                )
            except Exception as exc:
                error_rows = []
                warnings.append(f"{stage}: {exc}")
                yield self._build_sse(
                    "progress",
                    {"stage": stage, "message": "错误聚类超时或失败，已跳过。"},
                )
                await asyncio.sleep(0)

            stage = "collect_trace"
            trace_row = None
            if request_id:
                try:
                    trace_row = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._collect_request_trace_evidence,
                            request_id=request_id,
                            preferred_container=str(context_overrides.get("container") or "").strip() or None,
                        ),
                        timeout=DEFAULT_STAGE_TIMEOUT_S,
                    )
                except Exception as exc:
                    warnings.append(f"{stage}: {exc}")
                    yield self._build_sse(
                        "progress",
                        {"stage": stage, "message": "指定请求追踪超时或失败，已跳过。"},
                    )
                    await asyncio.sleep(0)
            stage = "collect_tool_trace"
            yield self._build_sse(
                "progress",
                {"stage": stage, "message": "正在联查工具调用链路..."},
            )
            await asyncio.sleep(0)
            try:
                tool_call_row = await asyncio.wait_for(
                    asyncio.to_thread(self._collect_tool_call_evidence, trace_row),
                    timeout=DEFAULT_STAGE_TIMEOUT_S,
                )
            except Exception as exc:
                tool_call_row = None
                warnings.append(f"{stage}: {exc}")
                yield self._build_sse(
                    "progress",
                    {"stage": stage, "message": "工具调用联查超时或失败，已跳过。"},
                )
                await asyncio.sleep(0)

            evidence_payload = {
                "scope": {
                    "lookback_days": DEFAULT_LOOKBACK_DAYS,
                    "timezone": self.settings.default_tz,
                    "main_containers": sorted(MAIN_FLOW_CONTAINERS),
                    "slow_threshold_s": DEFAULT_SLOW_THRESHOLD_S,
                },
                "intent": intent,
                "request_id": request_id,
                "warnings": warnings,
                "overview_snapshot": overview_rows,
                "slow_calls_top": slow_rows,
                "error_clusters_top": error_rows,
                "request_trace": trace_row,
                "tool_call_trace": tool_call_row,
            }
            stage = "emit_evidence"
            yield self._build_sse("evidence", evidence_payload)

            actions = self._build_actions(slow_rows=slow_rows, error_rows=error_rows, trace_row=trace_row)

            stage = "resolve_model_config"
            model_cfg = self._resolve_model_config()
            stage = "build_prompt"
            model_messages = self._build_model_messages(
                message=message,
                intent=intent,
                evidence_payload=evidence_payload,
                history=history,
            )

            stage = "model_call"
            yield self._build_sse(
                "progress",
                {"stage": stage, "message": "证据收集完成，正在生成诊断结论..."},
            )
            await asyncio.sleep(0)
            streamed_chunks = 0
            stream_error: Optional[Exception] = None
            try:
                for piece in self._stream_openai_compatible(
                    model_cfg["base_url"],
                    model_cfg["model"],
                    model_messages,
                ):
                    if not piece:
                        continue
                    streamed_chunks += 1
                    stage = "emit_tokens"
                    yield self._build_sse("token", {"text": piece})
                    await asyncio.sleep(0)
            except Exception as exc:
                stream_error = exc

            if streamed_chunks == 0:
                # 回退到非流式，保障兼容不支持 stream 的模型网关
                try:
                    model_text = self.completion_client(
                        model_cfg["base_url"],
                        model_cfg["model"],
                        model_messages,
                    )
                except Exception:
                    if stream_error is not None:
                        raise stream_error
                    raise

                stage = "emit_tokens"
                for chunk in self._split_text_chunks(model_text):
                    yield self._build_sse("token", {"text": chunk})
                    await asyncio.sleep(0)
            elif stream_error is not None:
                raise RuntimeError(f"模型流式中断: {stream_error}") from stream_error

            stage = "emit_actions"
            yield self._build_sse("actions", {"items": actions})
            stage = "emit_done"
            yield self._build_sse(
                "done",
                {
                    "conversation_id": conversation_id,
                    "intent": intent,
                    "request_id": request_id,
                },
            )
        except Exception as exc:
            yield self._build_sse("error", {"message": f"{stage}: {exc}"})
            yield self._build_sse("done", {"conversation_id": conversation_id})
