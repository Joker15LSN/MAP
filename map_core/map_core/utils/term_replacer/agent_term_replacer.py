from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from loguru import logger
from pydantic import BaseModel

from .query_term_replacer import replace_query_terms

GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE = "MASTER"


class _QueryRequestLike(Protocol):
    query: str
    original_query: str | None

    def model_copy(self, *, update: dict[str, Any]) -> "_QueryRequestLike": ...


class _AgentRequestLike(_QueryRequestLike, Protocol):
    extra: dict[str, Any]


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump()
    return None


def _pair_list_to_map(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        return {}

    resolved: dict[str, str] = {}
    duplicate_sources: list[str] = []
    for item in value:
        payload = _as_mapping(item)
        if payload is None:
            continue
        source = payload.get("source")
        target = payload.get("target")
        if not isinstance(source, list):
            continue
        if not isinstance(target, str):
            continue
        for source_item in source:
            if not isinstance(source_item, str) or not source_item:
                continue
            if source_item in resolved:
                duplicate_sources.append(source_item)
                continue
            resolved[source_item] = target
    if duplicate_sources:
        logger.warning(
            "Merged term_replacements contains duplicate sources. "
            "duplicate_count={}, duplicate_sources={}. Keeping first occurrence.",
            len(duplicate_sources),
            sorted(set(duplicate_sources))[:20],
        )
    return resolved


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _find_agent_configs(
    raw_configs: Any,
    *,
    agent_code: str,
) -> list[Mapping[str, Any]]:
    if not isinstance(raw_configs, list):
        return []

    resolved: list[Mapping[str, Any]] = []
    for raw_config in raw_configs:
        config = _as_mapping(raw_config)
        if config is None:
            continue
        config_agent_code = config.get("agent_code")
        if isinstance(config_agent_code, list) and agent_code in config_agent_code:
            resolved.append(config)
    return resolved


def _merge_string_lists(values: list[Any]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _string_list(value):
            if item in seen:
                continue
            resolved.append(item)
            seen.add(item)
    return resolved


def _merge_agent_configs(configs: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not configs:
        return None

    replacements: list[Any] = []
    translations: list[Any] = []
    protected_terms_values: list[Any] = []
    enable_translations = False

    for config in configs:
        raw_replacements = config.get("replacements")
        if isinstance(raw_replacements, list):
            replacements.extend(raw_replacements)

        protected_terms_values.append(config.get("protected_terms"))

        if bool(config.get("enable_translations", False)):
            raw_translations = config.get("translations")
            if isinstance(raw_translations, list):
                translations.extend(raw_translations)
            enable_translations = True

    return {
        "replacements": replacements,
        "protected_terms": _merge_string_lists(protected_terms_values),
        "translations": translations,
        "enable_translations": enable_translations,
    }


def _replace_request_query_with_config(
    request: _QueryRequestLike,
    *,
    agent_code: str,
    agent_config: Mapping[str, Any],
    extra: dict[str, Any] | None = None,
) -> _QueryRequestLike:
    result = replace_query_terms(
        query=request.query,
        replacements=_pair_list_to_map(agent_config.get("replacements")),
        protected_terms=_string_list(agent_config.get("protected_terms")),
        translations=_pair_list_to_map(agent_config.get("translations")),
        enable_translations=bool(agent_config.get("enable_translations", False)),
        enabled=True,
    )
    if not result.changed:
        return request

    original_query = request.original_query or request.query
    update: dict[str, Any] = {
        "query": result.query,
        "original_query": original_query,
    }
    if extra is not None:
        replacement_meta = {
            "agent_code": agent_code,
            "original_query": result.original_query,
            "query": result.query,
            "applied_replacements": result.applied_replacements,
        }
        updated_extra = dict(extra)
        updated_extra["query_term_replacement"] = replacement_meta
        update["extra"] = updated_extra
    return request.model_copy(update=update)


def replace_request_query_for_agent(
    request: _AgentRequestLike,
    *,
    agent_code: str,
) -> _AgentRequestLike:
    extra = request.extra or {}
    if not extra.get("query_term_replacer_enabled"):
        return request

    agent_config = _merge_agent_configs(
        _find_agent_configs(
            extra.get("term_replacements"),
            agent_code=agent_code,
        )
    )
    if agent_config is None:
        return request

    return cast(
        _AgentRequestLike,
        _replace_request_query_with_config(
            request,
            agent_code=agent_code,
            agent_config=agent_config,
            extra=extra,
        ),
    )


def replace_request_query_for_global_domain(
    request: _QueryRequestLike,
) -> _QueryRequestLike:
    if not getattr(request, "query_term_replacer_enabled", False):
        return request

    agent_config = _merge_agent_configs(
        _find_agent_configs(
            getattr(request, "term_replacements", None),
            agent_code=GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE,
        )
    )
    if agent_config is None:
        return request

    return _replace_request_query_with_config(
        request,
        agent_code=GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE,
        agent_config=agent_config,
    )
