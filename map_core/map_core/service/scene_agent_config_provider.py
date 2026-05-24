from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..schema.global_domain_schema import (
    AgentTermReplacementSchema,
    SceneAgentConfigSchema,
)
from .agent.agent_mapping import SceneAgentConfig
from .agent.base import AgentRequest

MAX_LOGGED_LOOKUP_PAYLOAD_CHARS = 30000


class SceneAgentConfigLookupRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    staff_code: str
    backend_env: str
    scene_agent_config_refs: list[str] = Field()

    @field_validator("scene_agent_config_refs")
    @classmethod
    def validate_scene_agent_config_refs(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            cleaned = item.strip()
            if not cleaned:
                raise ValueError("scene_agent_config_refs contains empty ref")
            if cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
        return normalized


class SceneAgentConfigLookupResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene_agent_configs: dict[str, SceneAgentConfigSchema] = Field()
    tool_context: dict[str, Any]
    term_replacements: list[AgentTermReplacementSchema] | None = None

    @staticmethod
    def _dedupe_replacement_pairs(
        value: Any,
        *,
        agent_code: Any,
        field_name: str,
    ) -> Any:
        if not isinstance(value, list):
            return value

        seen_sources: set[str] = set()
        duplicate_sources: list[str] = []
        normalized_pairs: list[Any] = []
        for raw_pair in value:
            if not isinstance(raw_pair, dict):
                normalized_pairs.append(raw_pair)
                continue

            raw_sources = raw_pair.get("source")
            if not isinstance(raw_sources, list):
                normalized_pairs.append(raw_pair)
                continue

            normalized_sources: list[str] = []
            for raw_source in raw_sources:
                if not isinstance(raw_source, str):
                    normalized_sources.append(raw_source)
                    continue
                source = raw_source.strip()
                if source in seen_sources:
                    duplicate_sources.append(source)
                    continue
                seen_sources.add(source)
                normalized_sources.append(raw_source)

            if not normalized_sources:
                continue
            normalized_pair = dict(raw_pair)
            normalized_pair["source"] = normalized_sources
            normalized_pairs.append(normalized_pair)

        if duplicate_sources:
            logger.warning(
                "External scene agent config term_replacements contains duplicate "
                "sources. agent_code={}, field={}, duplicate_count={}, "
                "duplicate_sources={}. Keeping first occurrence.",
                agent_code,
                field_name,
                len(duplicate_sources),
                sorted(set(duplicate_sources))[:20],
            )

        return normalized_pairs

    @field_validator("term_replacements", mode="before")
    @classmethod
    def normalize_external_term_replacements(cls, value: Any) -> Any:
        """External config can contain repeated term rows; keep first occurrence."""
        if not isinstance(value, list):
            return value

        normalized_configs: list[Any] = []
        for raw_config in value:
            if not isinstance(raw_config, dict):
                normalized_configs.append(raw_config)
                continue

            normalized_config = dict(raw_config)
            agent_code = normalized_config.get("agent_code")
            for field_name in ("replacements", "translations"):
                if field_name in normalized_config:
                    normalized_config[field_name] = cls._dedupe_replacement_pairs(
                        normalized_config[field_name],
                        agent_code=agent_code,
                        field_name=field_name,
                    )
            normalized_configs.append(normalized_config)

        return normalized_configs


class SceneAgentConfigFetchResult(BaseModel):
    scene_agent_configs: dict[str, SceneAgentConfig]
    tool_context: dict[str, Any]
    term_replacements: list[AgentTermReplacementSchema] | None = None


class SceneAgentConfigProvider:
    """Fetch scene agent configs from external service by refs."""

    # LOOKUP_PATH = "/msService/chatadmin/hcaChatAgent/algo/queryAgentConfig"
    # LOOKUP_PATH = "/msService/public/chatadmin/algo/queryAgentConfig"
    LOOKUP_PATH = "/msService/public/map-chatadmin/algo/queryAgentConfig"

    def __init__(
        self,
        *,
        endpoint: str = "",
        timeout_s: float = 5.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    @staticmethod
    def _extract_lookup_payload(response_json: Any) -> dict[str, Any]:
        """Extract the actual payload dict from the response JSON, which may be wrapped in a "data" field or be the top-level object itself."""
        if not isinstance(response_json, dict):
            raise ValueError("scene agent config response must be a JSON object")

        data = response_json.get("data")
        if isinstance(data, dict):
            return data

        return response_json

    @classmethod
    def _build_lookup_endpoint(cls, endpoint: str) -> str:
        normalized_endpoint = endpoint.strip()
        if not normalized_endpoint:
            return ""

        url = httpx.URL(normalized_endpoint)
        normalized_path = url.path.rstrip("/")
        final_path = (
            normalized_path
            if normalized_path.endswith(cls.LOOKUP_PATH)
            else normalized_path + cls.LOOKUP_PATH
        )
        return str(url.copy_with(path=final_path))

    async def fetch_by_refs(
        self,
        refs: list[str],
        request: AgentRequest,
        tool_context: dict[str, Any] | None = None,
    ) -> SceneAgentConfigFetchResult:
        request_endpoint = str(
            request.extra.get("backend_env_base_url", "missing")
        ).strip()
        endpoint = (
            request_endpoint
            if request_endpoint and request_endpoint != "missing"
            else self.endpoint
        )

        if not endpoint:
            message = (
                "Scene agent config endpoint is empty. "
                "Provide backend_env_base_url in request body or configure SceneAgentConfigProvider.endpoint before using scene_agent_config_refs."
            )
            logger.error(message)
            raise ValueError(message)
        endpoint = self._build_lookup_endpoint(endpoint)

        backend_env = request.extra.get("backend_env", "missing")
        payload = SceneAgentConfigLookupRequest.model_validate(
            {
                "staff_code": request.staff_code,
                "backend_env": str(backend_env),
                "scene_agent_config_refs": refs,
            }
        ).model_dump()
        logged_payload = str(payload)[:MAX_LOGGED_LOOKUP_PAYLOAD_CHARS]
        logger.debug(
            f"Fetching scene agent configs by refs. endpoint={endpoint}, payload={logged_payload}"
        )

        headers: dict[str, str] = {}
        request_id = request.extra.get("request_id")
        session_id = request.extra.get("session_id")
        if (
            isinstance(request_id, str)
            and request_id.strip()
            and request_id != "missing"
        ):
            headers["X-Request-ID"] = request_id
        if isinstance(session_id, str) and session_id.strip() and session_id != "-":
            headers["X-Session-ID"] = session_id
        request_token = request.extra.get("request_token")
        if isinstance(request_token, str) and request_token.strip():
            headers["Authorization"] = request_token
            headers["X-Request-Token"] = request_token
        x_userid = request.extra.get("x_userid", "missing")
        headers["X-UserId"] = x_userid if isinstance(x_userid, str) else str(x_userid)
        x_username = request.extra.get("x_username", "missing")
        headers["X-UserName"] = (
            x_username if isinstance(x_username, str) else str(x_username)
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers or None,
                )
                response.raise_for_status()
        except Exception as exc:
            logger.exception(
                f"Failed to fetch scene agent configs by refs. endpoint={endpoint}, refs={refs}"
            )
            raise

        logger.debug(
            f"Fetched scene agent configs by refs. endpoint={endpoint}, "
            f"status_code={response.status_code}, response_headers={dict(response.headers)}, "
            f"response_body={response.text}"
        )

        response_json = response.json()
        lookup_payload = self._extract_lookup_payload(response_json)
        try:
            parsed = SceneAgentConfigLookupResponse.model_validate(lookup_payload)
        except ValidationError as exc:
            logger.error(
                "Invalid scene agent config response payload. "
                f"endpoint={endpoint}, refs={refs}, errors={exc.errors()}, "
                f"response_json={response_json}, lookup_payload={lookup_payload}"
            )
            raise

        missing_names = sorted(
            set(payload["scene_agent_config_refs"])
            - set(parsed.scene_agent_configs.keys())
        )
        if missing_names:
            message = (
                "External scene agent config response missing requested agents: "
                + ", ".join(missing_names)
            )
            logger.error(message)
            raise ValueError(message)

        scene_agent_configs = {
            name: SceneAgentConfig(**cfg.model_dump())
            for name, cfg in parsed.scene_agent_configs.items()
        }

        return SceneAgentConfigFetchResult(
            scene_agent_configs=scene_agent_configs,
            tool_context=parsed.tool_context,
            term_replacements=parsed.term_replacements,
        )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch scene agent configs by agent_code refs."
    )
    parser.add_argument(
        "refs",
        nargs="+",
        help="Agent codes to fetch, for example: Operations Market_Assistant",
    )
    parser.add_argument(
        "--backend-env-base-url",
        default="http://10.48.1.120:8080",
        help="Backend base URL used to build the config lookup endpoint.",
    )
    parser.add_argument("--backend-env", default="RELEASE_STATE")
    parser.add_argument("--staff-code", default="0120240487")
    parser.add_argument("--query", default="test query")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--request-token", default="Bearer lcXNZGHIkhm5mj9Csyadb")
    parser.add_argument("--x-userid", default="missing")
    parser.add_argument("--x-username", default="zhuyihan")
    parser.add_argument("--timeout-s", type=float, default=5.0)
    return parser


async def _run_cli() -> None:
    args = _build_cli_parser().parse_args()
    extra = {
        "backend_env_base_url": args.backend_env_base_url,
        "backend_env": args.backend_env,
        "x_userid": args.x_userid,
        "x_username": args.x_username,
    }
    for key in ("request_id", "session_id", "request_token"):
        value = getattr(args, key)
        if value:
            extra[key] = value

    request = AgentRequest(
        query=args.query,
        staff_code=args.staff_code,
        extra=extra,
    )
    provider = SceneAgentConfigProvider(timeout_s=args.timeout_s)
    result = await provider.fetch_by_refs(
        refs=args.refs,
        request=request,
        tool_context=None,
    )
    print(
        json.dumps(
            {
                "scene_agent_configs": {
                    name: config.model_dump()
                    for name, config in result.scene_agent_configs.items()
                },
                "tool_context": result.tool_context,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(_run_cli())
