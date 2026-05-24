from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Mapping

import httpx
from loguru import logger

from ..config import CHART_PLOTTING_API_URL
from .agent.base import AgentResult

CHART_CANDIDATE_TOOL_NAMES = frozenset({"wenshu_agent", "ask_database_agent"})
TERMINATE_TOOL_NAME = "terminate"

DEFAULT_CHART_PLOTTING_API_URL = CHART_PLOTTING_API_URL
DEFAULT_CHART_PLOTTING_API_TIMEOUT_S = float(
    os.getenv("CHART_PLOTTING_API_TIMEOUT_S", "10")
)
DEFAULT_CHART_PLOTTING_API_AUTH_TOKEN = (
    os.getenv("CHART_PLOTTING_API_AUTH_TOKEN", "").strip() or None
)


def _try_parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _has_non_empty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_non_empty_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_non_empty_value(item) for item in value)
    return True


def _is_successful_non_empty_tool_result(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("success") is not True:
        return False

    meaningful_result = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "success",
            "name",
            "error",
            "exit",
            "meta_data",
            "extra_result",
        }
    }
    return _has_non_empty_value(meaningful_result)


def _has_ask_database_chart_source_data(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False

    data_source = value.get("data_source")
    if not isinstance(data_source, Mapping):
        return False

    data_items = data_source.get("data")
    if not isinstance(data_items, list):
        return False

    for item in data_items:
        if isinstance(item, Mapping) and _has_non_empty_value(item.get("data")):
            return True
    return False


CHART_TOOL_RESULT_EXTRA_VALIDATORS: Mapping[str, Callable[[Any], bool]] = {
    "ask_database_agent": _has_ask_database_chart_source_data,
}


def _is_chart_plotting_candidate_tool_result(tool_name: str, value: Any) -> bool:
    if not _is_successful_non_empty_tool_result(value):
        return False

    extra_validator = CHART_TOOL_RESULT_EXTRA_VALIDATORS.get(tool_name)
    if extra_validator is None:
        return True
    return extra_validator(value)


def _extract_result_name_and_history(result: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(result, AgentResult):
        agent_name = result.name
        data_source = result.data_source
    elif isinstance(result, dict):
        agent_name = str(result.get("name") or "unknown")
        data_source = result.get("data_source")
    else:
        agent_name = str(getattr(result, "name", "unknown"))
        data_source = getattr(result, "data_source", None)

    if not isinstance(data_source, dict):
        return agent_name, []

    history = data_source.get("history")
    if not isinstance(history, list):
        return agent_name, []

    normalized_history = [item for item in history if isinstance(item, dict)]
    return agent_name, normalized_history


def build_chart_plotting_payload(
    *,
    request_id: str,
    state_id: str,
    session_id: str | None,
    query: str,
    staff_code: str,
    backend_env: str,
    backend_env_base_url: str,
    dispatch_results: list[Any],
) -> dict[str, Any] | None:
    collected_calls: list[dict[str, Any]] = []
    candidate_calls: list[dict[str, Any]] = []
    terminate_call_count = 0

    logger.info(
        "Evaluating chart plotting trigger: request_id={}, dispatch_result_count={}",
        request_id,
        len(dispatch_results),
    )

    for result in dispatch_results:
        agent_name, history = _extract_result_name_and_history(result)
        if not history:
            logger.debug(
                "Chart plotting skipped agent history: request_id={}, agent_code={}, reason=no_history",
                request_id,
                agent_name,
            )
            continue

        tool_results_by_id: dict[str, Any] = {}
        for message in history:
            if message.get("role") != "tool":
                continue
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            tool_results_by_id[tool_call_id] = _try_parse_json(message.get("content"))

        for message in history:
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue

            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                tool_call_id = call.get("id")
                function_payload = call.get("function")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    continue
                if not isinstance(function_payload, dict):
                    continue

                tool_name = function_payload.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                if tool_name == TERMINATE_TOOL_NAME:
                    terminate_call_count += 1
                    continue

                call_record = {
                    "agent_code": agent_name,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "tool_args": _try_parse_json(function_payload.get("arguments")),
                    "tool_result": tool_results_by_id.get(tool_call_id),
                }
                collected_calls.append(call_record)
                if (
                    tool_name in CHART_CANDIDATE_TOOL_NAMES
                    and _is_chart_plotting_candidate_tool_result(
                        tool_name, call_record["tool_result"]
                    )
                ):
                    candidate_calls.append(call_record)

    if not collected_calls:
        logger.info(
            "Chart plotting not triggered: request_id={}, reason=no_effective_tool_calls, terminate_call_count={}",
            request_id,
            terminate_call_count,
        )
        return None
    if not candidate_calls:
        logger.info(
            "Chart plotting not triggered: request_id={}, reason=no_chart_candidate_tool_calls, total_tool_call_count={}, terminate_call_count={}",
            request_id,
            len(collected_calls),
            terminate_call_count,
        )
        return None
    payload: dict[str, Any] = {
        "request_id": request_id,
        "state_id": state_id,
        "query": query,
        "staff_code": staff_code,
        "backend_env": backend_env,
        "backend_env_base_url": backend_env_base_url,
        "target_tool": str(candidate_calls[0]["tool_name"]),
        "tool_calls": candidate_calls,
    }
    if session_id:
        payload["session_id"] = session_id

    logger.info(
        "Chart plotting triggered: request_id={}, target_tool={}, candidate_tool_call_count={}, candidate_tool_names={}, total_tool_call_count={}",
        request_id,
        payload["target_tool"],
        len(candidate_calls),
        [str(call["tool_name"]) for call in candidate_calls],
        len(collected_calls),
    )
    return payload


async def post_chart_plotting_payload(
    payload: Mapping[str, Any],
    *,
    api_url: str | None = None,
    timeout_s: float = DEFAULT_CHART_PLOTTING_API_TIMEOUT_S,
    auth_token: str | None = DEFAULT_CHART_PLOTTING_API_AUTH_TOKEN,
) -> bool:
    resolved_api_url = (
        api_url.strip() if isinstance(api_url, str) else DEFAULT_CHART_PLOTTING_API_URL
    )
    if not resolved_api_url:
        logger.warning(
            "Chart plotting is enabled but CHART_PLOTTING_API_URL is missing; skip external call"
        )
        return False

    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = auth_token

    try:
        request_body = dict(payload)
        serialized_request_body = json.dumps(
            request_body,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        logger.debug(
            "Chart plotting request body: {}",
            serialized_request_body,
        )
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
            response = await client.post(
                resolved_api_url,
                json=request_body,
                headers=headers,
            )
            logger.info(
                f"Chart plotting API responded with status code {response.status_code}, response body: {response.text}"
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("Chart plotting external API call failed: {}", exc)
        return False
    except Exception as exc:
        logger.error(
            "Chart plotting external API call raised unexpected error: {}", exc
        )
        return False


async def generate_and_persist_chart_plotting(
    *,
    request: Any,
    dispatch_results: list[Any],
    request_id: str,
    state_id: str,
    session_id: str | None,
    query: str,
    staff_code: str,
    request_token: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not getattr(request, "chart_plotting_enabled", False):
        return None

    logger.debug(
        "Chart plotting enabled, building payload: request_id={}, dispatch_result_count={}",
        request_id,
        len(dispatch_results),
    )

    if payload is None:
        payload = build_chart_plotting_payload(
            request_id=request_id,
            state_id=state_id,
            session_id=session_id,
            query=query,
            staff_code=staff_code,
            backend_env=getattr(request, "backend_env", "EDITORIAL_STATE"),
            backend_env_base_url=getattr(request, "backend_env_base_url", "missing"),
            dispatch_results=dispatch_results,
        )
    if payload is None:
        logger.debug(
            "Chart plotting skipped: dispatch tools do not include chart candidate tools"
        )
        return None

    posted = await post_chart_plotting_payload(
        payload,
        auth_token=request_token,
    )
    if not posted:
        logger.warning(
            "Chart plotting external request failed: request_id={}",
            request_id,
        )
        return {"status": "failed"}

    logger.info("Chart plotting request sent: request_id={}", request_id)
    return {
        "status": "success",
        "request_sent": True,
    }


async def collect_chart_plotting_meta(
    chart_task: Any,
    *,
    request_id: str,
    timeout_s: float | None = None,
) -> dict[str, Any] | None:
    try:
        if timeout_s is None:
            return await chart_task
        return await asyncio.wait_for(chart_task, timeout=timeout_s)
    except TimeoutError:
        logger.warning(
            "Chart plotting task timed out during stream finalization: request_id={}, budget_s={}",
            request_id,
            timeout_s,
        )
        if not chart_task.done():
            chart_task.cancel()
        return {
            "status": "failed",
            "reason": "timeout",
            "budget_s": timeout_s,
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Chart plotting task failed: {}", exc)
        return {"status": "failed", "error": str(exc)}
