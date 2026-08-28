import asyncio
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from loguru import logger
from pydantic import ValidationError

from ..schema.agent_schema import Message
from ..schema.global_domain_schema import (
    EnabledAgentConfigSchema,
    GlobalDomainChatSchema,
    GlobalDomainChatV3Schema,
    SceneSelectionConfigSchema,
)
from ..schema.scene_classification_schema import (
    BigSceneClassificationResult,
    SceneClassificationResult,
    SceneItem,
    SubSceneResult,
)
from ..schema.scene_registry import (
    SCENE_REGISTRY,
    SUB_SCENES,
    SceneConfig,
    SceneRegistrySchema,
    build_big_scene_to_sub_scenes,
    build_scene_catalog_text,
    build_sub_scene_descriptions,
    normalize_scene_registry,
)
from ..service.prompt.scene_classification_prompt import (
    BIG_SCENE_SYSTEM_PROMPT_TEMPLATE,
    SUB_SCENE_CLASSIFICATION_PROMPT,
    SUB_SCENE_SYSTEM_PROMPT,
)
from ..utils.llm_engine import LLMEngine, LLMResponse
from ..utils.llm_trace_context import llm_trace_context

GlobalDomainRequest = GlobalDomainChatSchema | GlobalDomainChatV3Schema


@dataclass(slots=True)
class SceneSelectionRuntimeConfig:
    scene_registry: dict[str, SceneConfig]
    big_scene_to_sub_scenes: dict[str, list[str]]
    sub_scene_descriptions: dict[str, str]
    big_scene_system_prompt: str
    sub_scene_user_prompt_template: str
    enabled_agent_codes: dict[str, EnabledAgentConfigSchema] | None = None


@dataclass(frozen=True, slots=True)
class ForcedAgentSelection:
    agent_code: str
    reason: str


class SceneSelectionOutcome:
    __slots__ = ("result", "token_usage")

    def __init__(
        self,
        *,
        result: SceneClassificationResult,
        token_usage: dict[str, int],
    ) -> None:
        self.result = result
        self.token_usage = token_usage


class SceneSelectionObserver(Protocol):
    def on_stage_start(self, stage: str, data: dict[str, Any]) -> None: ...

    def on_stage_end(
        self,
        stage: str,
        status: Literal["success", "failed"],
        data: dict[str, Any],
    ) -> None: ...


class SceneSelector:
    """Two-level business scene classification: big scenes and sub-scenes."""

    DEFAULT_BIG_SCENE_USER_PROMPT_TEMPLATE = (
        "{history_context}\n请结合上述历史对话背景（如果你认为有关联的话），"
        "对以下用户问题进行业务场景分类。\n用户问题：{query}"
    )
    DEFAULT_SUB_SCENE_USER_PROMPT_TEMPLATE = (
        "{history_context}" + SUB_SCENE_CLASSIFICATION_PROMPT
    )
    KB_FILE_TRIGGERED_AGENT_CODES: tuple[str, ...] = (
        "General_Assistant",
    )  # forced enable when uploaded_kb_files is present

    def __init__(
        self,
        llm: LLMEngine,
        big_scene_system_prompt_template: str | None = None,
        sub_scene_user_prompt_template: str | None = None,
        history_turn_limit: int = 1,
    ) -> None:
        self._llm = llm
        self._big_scene_system_prompt_template = (
            big_scene_system_prompt_template or BIG_SCENE_SYSTEM_PROMPT_TEMPLATE
        )
        self._big_scene_user_prompt_template = (
            self.DEFAULT_BIG_SCENE_USER_PROMPT_TEMPLATE
        )
        self._sub_scene_user_prompt_template = (
            sub_scene_user_prompt_template
            or self.DEFAULT_SUB_SCENE_USER_PROMPT_TEMPLATE
        )
        self._history_turn_limit = max(0, history_turn_limit)

    @staticmethod
    def _truncate_text(text: str | None, limit: int = 800) -> str:
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return f"{text[:limit]}... <truncated>"

    @staticmethod
    def _empty_scene_result() -> SceneClassificationResult:
        return SceneClassificationResult.model_construct(big_scenes=[], sub_scenes=[])

    @classmethod
    def _empty_scene_outcome(cls) -> SceneSelectionOutcome:
        return SceneSelectionOutcome(result=cls._empty_scene_result(), token_usage={})

    @staticmethod
    def has_selectable_agents(config: SceneSelectionRuntimeConfig) -> bool:
        return any(payload["sub_scenes"] for payload in config.scene_registry.values())

    @staticmethod
    def _merge_token_usage(
        target: dict[str, int],
        source: Mapping[str, Any] | None,
    ) -> None:
        if not source:
            return
        for key, value in source.items():
            if isinstance(value, int):
                target[key] = target.get(key, 0) + value

    @staticmethod
    def _notify_stage_start(
        observer: SceneSelectionObserver | None,
        stage: str,
        data: dict[str, Any],
    ) -> None:
        if observer is None:
            return
        try:
            observer.on_stage_start(stage, data)
        except Exception as exc:
            logger.warning(f"Scene selection observer start hook failed: {exc}")

    @staticmethod
    def _notify_stage_end(
        observer: SceneSelectionObserver | None,
        stage: str,
        status: Literal["success", "failed"],
        data: dict[str, Any],
    ) -> None:
        if observer is None:
            return
        try:
            observer.on_stage_end(stage, status, data)
        except Exception as exc:
            logger.warning(f"Scene selection observer end hook failed: {exc}")

    @classmethod
    def _normalize_enabled_agent_codes(
        cls,
        enabled_agent_codes: Mapping[str, EnabledAgentConfigSchema] | None,
    ) -> dict[str, EnabledAgentConfigSchema] | None:
        if enabled_agent_codes is None:
            return None

        known_sub_scenes = set(SUB_SCENES)
        normalized: dict[str, EnabledAgentConfigSchema] = {}
        for raw_code, raw_config in enabled_agent_codes.items():
            cleaned_code = raw_code.strip()
            if not cleaned_code:
                continue
            if cleaned_code in normalized:
                continue
            normalized[cleaned_code] = raw_config

        unknown_codes = [code for code in normalized if code not in known_sub_scenes]
        if unknown_codes:
            logger.warning(f"Ignoring unknown enabled_agent_codes: {unknown_codes}")
            normalized = {
                code: agent_name
                for code, agent_name in normalized.items()
                if code in known_sub_scenes
            }

        return normalized

    @classmethod
    def _filter_scene_registry_by_agent_codes(
        cls,
        scene_registry: dict[str, SceneConfig],
        enabled_agent_codes: Mapping[str, EnabledAgentConfigSchema] | None,
    ) -> tuple[dict[str, SceneConfig], dict[str, EnabledAgentConfigSchema] | None]:
        normalized_enabled = cls._normalize_enabled_agent_codes(enabled_agent_codes)
        if normalized_enabled is None:
            return scene_registry, normalized_enabled
        if not normalized_enabled:
            logger.info(
                "enabled_agent_codes is explicitly empty; fallback agents remain enabled"
            )
            return {}, normalized_enabled

        allowed = set(normalized_enabled)
        filtered: dict[str, SceneConfig] = {}
        for big_scene, payload in scene_registry.items():
            filtered_sub_scenes: dict[str, str] = {}
            for code, description in payload["sub_scenes"].items():
                if code not in allowed:
                    continue
                config = normalized_enabled[code]
                filtered_sub_scenes[code] = config.agent_description
            if not filtered_sub_scenes:
                continue
            filtered[big_scene] = {
                "description": payload["description"],
                "sub_scenes": filtered_sub_scenes,
            }

        if filtered:
            return filtered, normalized_enabled

        logger.warning(
            "enabled_agent_codes produced empty scene registry after filtering; fallback to full registry"
        )
        return scene_registry, normalized_enabled

    @staticmethod
    def _map_description_term_to_sub_scene(
        big_scene: str,
        candidate: str,
        scene_registry: dict[str, SceneConfig],
    ) -> str | None:
        payload = scene_registry.get(big_scene)
        if not payload:
            return None

        term = candidate.strip()
        if not term:
            return None

        matches = [
            sub_scene
            for sub_scene, description in payload["sub_scenes"].items()
            if term == description or term in description
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    @classmethod
    def _normalize_sub_scene_labels(
        cls,
        big_scene: str,
        sub_scenes: Iterable[str],
        scene_registry: dict[str, SceneConfig],
        big_scene_to_sub_scenes: dict[str, list[str]],
    ) -> tuple[list[str], list[str], dict[str, str]]:
        valid_sub_scenes = set(big_scene_to_sub_scenes.get(big_scene, []))
        normalized: list[str] = []
        invalid: list[str] = []
        mapped: dict[str, str] = {}

        for sub_scene in sub_scenes:
            if sub_scene in valid_sub_scenes:
                normalized.append(sub_scene)
                continue

            mapped_sub_scene = cls._map_description_term_to_sub_scene(
                big_scene, sub_scene, scene_registry
            )

            if mapped_sub_scene is not None:
                normalized.append(mapped_sub_scene)
                mapped[sub_scene] = mapped_sub_scene
                continue

            invalid.append(sub_scene)

        deduped: list[str] = []
        seen: set[str] = set()
        for sub_scene in normalized:
            if sub_scene in seen:
                continue
            seen.add(sub_scene)
            deduped.append(sub_scene)
        return deduped, invalid, mapped

    @staticmethod
    def _normalize_big_scene_labels(
        result: BigSceneClassificationResult,
        allowed_big_scenes: Iterable[str],
    ) -> tuple[BigSceneClassificationResult, list[str]]:
        allowed = set(allowed_big_scenes)
        dropped: list[str] = []
        normalized_items = []

        for item in result.big_scenes:
            if item.big_scene not in allowed:
                dropped.append(item.big_scene)
                continue
            normalized_items.append(item)

        return result.model_copy(update={"big_scenes": normalized_items}), dropped

    @staticmethod
    def _build_big_scene_json_schema(allowed_big_scenes: list[str]) -> dict:
        schema = BigSceneClassificationResult.model_json_schema()

        scene_item_schema = (
            schema.get("properties", {}).get("big_scenes", {}).get("items", {})
        )
        if "$ref" in scene_item_schema:
            ref = str(scene_item_schema["$ref"])
            def_name = ref.rsplit("/", 1)[-1]
            scene_item_schema = schema.get("$defs", {}).get(def_name, {})

        big_scene_prop = scene_item_schema.get("properties", {}).get("big_scene", {})
        big_scene_prop["enum"] = allowed_big_scenes

        return schema

    @staticmethod
    def _build_sub_scene_json_schema(
        big_scene: str,
        big_scene_to_sub_scenes: dict[str, list[str]],
    ) -> dict:
        allowed_sub_scenes = big_scene_to_sub_scenes.get(big_scene, [])

        schema = SubSceneResult.model_json_schema()

        if "properties" in schema:
            if "big_scene" in schema["properties"]:
                schema["properties"]["big_scene"]["enum"] = [big_scene]
            if "sub_scenes" in schema["properties"]:
                sub_scenes_prop = schema["properties"]["sub_scenes"]
                if "items" in sub_scenes_prop:
                    sub_scenes_prop["items"]["enum"] = allowed_sub_scenes

        return schema

    @staticmethod
    def _build_history_context(
        history: Sequence[Message | dict[str, Any]] | None,
        history_turn_limit: int = 2,
    ) -> str:
        if not history or history_turn_limit <= 0:
            return ""
        message_limit = max(1, history_turn_limit) * 2
        latest_turn = (
            history[-message_limit:] if len(history) >= message_limit else history
        )
        lines: list[str] = []
        for msg in latest_turn:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                lines.append(f"{msg['role']}: {msg['content']}")
            elif isinstance(msg, Message):
                lines.append(f"{msg.role}: {msg.content}")
        text = "\n".join(lines)
        return f"历史对话背景:\n{text}\n\n" if text else ""

    @staticmethod
    def _render_template(template: str, template_vars: dict[str, Any]) -> str:
        return template.format(**template_vars)

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return parsed if isinstance(parsed, dict) else {}

    def build_history_context(
        self,
        history: Sequence[Message | dict[str, Any]] | None,
    ) -> str:
        """Public wrapper used by orchestrators to avoid private-method coupling."""
        return self._build_history_context(
            history,
            history_turn_limit=self._history_turn_limit,
        )

    def _build_uploaded_file_appendix(self, request: GlobalDomainRequest) -> str | None:
        file_uploaded_appendix = None
        if request.uploaded_kb_files:
            file_descs = []
            for kb_file in request.uploaded_kb_files:
                file_descs.append(kb_file.file_name)
            file_uploaded_appendix = (
                f"用户当前已上传了文件: {'、'.join(file_descs)}，供你参考"
            )
        return file_uploaded_appendix

    def resolve_runtime_config(
        self,
        request: GlobalDomainRequest,
        enabled_agent_codes: Mapping[str, EnabledAgentConfigSchema] | None = None,
    ) -> SceneSelectionRuntimeConfig:
        scene_selection = cast(
            SceneSelectionConfigSchema | None,
            getattr(request, "scene_selection", None),
        )

        raw_scene_registry_input = getattr(scene_selection, "scene_registry", None)
        scene_registry_input = raw_scene_registry_input or SCENE_REGISTRY
        resolved_scene_registry = normalize_scene_registry(scene_registry_input)

        request_enabled_agent_codes = getattr(
            scene_selection, "enabled_agent_codes", None
        )
        merged_enabled_agent_codes: dict[str, EnabledAgentConfigSchema] = {}
        for code, agent_config in (enabled_agent_codes or {}).items():
            cleaned = code.strip()
            if not cleaned or cleaned in merged_enabled_agent_codes:
                continue
            merged_enabled_agent_codes[cleaned] = agent_config
        for code, agent_config in (request_enabled_agent_codes or {}).items():
            if code in merged_enabled_agent_codes:
                continue
            merged_enabled_agent_codes[code] = agent_config
        for forced_selection in self._collect_forced_agent_selections(request):
            if forced_selection.agent_code in merged_enabled_agent_codes:
                continue
            agent_description = forced_selection.agent_code
            forced_big_scene = self._find_big_scene_for_agent_code(
                forced_selection.agent_code,
                resolved_scene_registry,
            )
            if forced_big_scene is not None:
                agent_description = resolved_scene_registry[forced_big_scene][
                    "sub_scenes"
                ][forced_selection.agent_code]
            merged_enabled_agent_codes[forced_selection.agent_code] = (
                EnabledAgentConfigSchema(
                    agent_name=forced_selection.agent_code,
                    agent_description=agent_description,
                )
            )
        explicit_empty_whitelist = not merged_enabled_agent_codes and (
            (enabled_agent_codes is not None and not enabled_agent_codes)
            or (
                request_enabled_agent_codes is not None
                and not request_enabled_agent_codes
            )
        )

        filtered_scene_registry, resolved_enabled_agent_codes = (
            self._filter_scene_registry_by_agent_codes(
                resolved_scene_registry,
                merged_enabled_agent_codes
                if merged_enabled_agent_codes or explicit_empty_whitelist
                else None,
            )
        )

        big_scene_to_sub_scenes = build_big_scene_to_sub_scenes(filtered_scene_registry)
        sub_scene_descriptions = build_sub_scene_descriptions(filtered_scene_registry)
        scene_catalog_text = build_scene_catalog_text(filtered_scene_registry)

        big_scene_system_prompt_template = (
            getattr(scene_selection, "big_scene_system_prompt_template", None)
            or self._big_scene_system_prompt_template
        )
        sub_scene_user_prompt_template = (
            getattr(scene_selection, "sub_scene_user_prompt_template", None)
            or self._sub_scene_user_prompt_template
        )
        file_uploaded_appendix = self._build_uploaded_file_appendix(request=request)
        if file_uploaded_appendix:
            big_scene_system_prompt_template += f'\n{file_uploaded_appendix}'
            if '{uploaded_file_placeholder}' in sub_scene_user_prompt_template:
                # 存在format placeholder 则替换
                sub_scene_user_prompt_template =sub_scene_user_prompt_template.replace('{uploaded_file_placeholder}', file_uploaded_appendix)
            else:
                # 否则append
                sub_scene_user_prompt_template += f'\n{file_uploaded_appendix}'
        else:
            if '{uploaded_file_placeholder}' in sub_scene_user_prompt_template:
                # 替换为空字符串
                sub_scene_user_prompt_template =sub_scene_user_prompt_template.replace('{uploaded_file_placeholder}', '')


        try:
            big_scene_system_prompt = self._render_template(
                big_scene_system_prompt_template,
                {"scene_catalog_text": scene_catalog_text},
            )
        except KeyError as exc:
            raise ValueError(
                f"Invalid big_scene_system_prompt_template missing key: {exc}"
            )

        return SceneSelectionRuntimeConfig(
            scene_registry=filtered_scene_registry,
            big_scene_to_sub_scenes=big_scene_to_sub_scenes,
            sub_scene_descriptions=sub_scene_descriptions,
            big_scene_system_prompt=big_scene_system_prompt,
            sub_scene_user_prompt_template=sub_scene_user_prompt_template,
            enabled_agent_codes=resolved_enabled_agent_codes,
        )

    @classmethod
    def _collect_forced_agent_selections(
        cls,
        request: GlobalDomainRequest,
    ) -> list[ForcedAgentSelection]:
        forced: list[ForcedAgentSelection] = []
        if request.uploaded_kb_files:
            forced.extend(
                ForcedAgentSelection(
                    agent_code=agent_code,
                    reason="uploaded_kb_files is present",
                )
                for agent_code in cls.KB_FILE_TRIGGERED_AGENT_CODES
            )

        deduped: list[ForcedAgentSelection] = []
        seen: set[str] = set()
        for item in forced:
            if item.agent_code in seen:
                continue
            seen.add(item.agent_code)
            deduped.append(item)
        return deduped

    @staticmethod
    def _find_big_scene_for_agent_code(
        agent_code: str,
        scene_registry: Mapping[str, SceneConfig],
    ) -> str | None:
        for big_scene, payload in scene_registry.items():
            if agent_code in payload["sub_scenes"]:
                return big_scene
        return None

    @classmethod
    def _apply_forced_agent_selections(
        cls,
        result: SceneClassificationResult,
        request: GlobalDomainRequest,
        scene_registry: Mapping[str, SceneConfig],
    ) -> SceneClassificationResult:
        forced_selections = cls._collect_forced_agent_selections(request)
        if not forced_selections:
            return result

        big_scene_entries = list(result.big_scenes)
        sub_scene_entries = list(result.sub_scenes)
        selected_big_scenes = {item.big_scene for item in big_scene_entries}
        sub_scene_index = {
            item.big_scene: idx for idx, item in enumerate(sub_scene_entries)
        }

        for forced_selection in forced_selections:
            big_scene = cls._find_big_scene_for_agent_code(
                forced_selection.agent_code,
                scene_registry,
            )
            if big_scene is None:
                logger.warning(
                    "Failed to auto-enable agent_code "
                    f"{forced_selection.agent_code} because it is absent from scene_registry"
                )
                continue

            if big_scene not in selected_big_scenes:
                big_scene_entries.append(
                    SceneItem(
                        big_scene=big_scene,
                        confidence=1.0,
                        reason=f"Auto-selected because {forced_selection.reason}.",
                    )
                )
                selected_big_scenes.add(big_scene)

            existing_idx = sub_scene_index.get(big_scene)
            if existing_idx is None:
                sub_scene_entries.append(
                    SubSceneResult(
                        big_scene=big_scene,
                        sub_scenes=[forced_selection.agent_code],
                        confidence=1.0,
                        reason=f"Auto-enabled {forced_selection.agent_code} because {forced_selection.reason}.",
                    )
                )
                sub_scene_index[big_scene] = len(sub_scene_entries) - 1
                continue

            existing = sub_scene_entries[existing_idx]
            if forced_selection.agent_code in existing.sub_scenes:
                continue
            sub_scene_entries[existing_idx] = existing.model_copy(
                update={
                    "sub_scenes": [*existing.sub_scenes, forced_selection.agent_code]
                }
            )

        return result.model_copy(
            update={
                "big_scenes": big_scene_entries,
                "sub_scenes": sub_scene_entries,
            }
        )

    def _finalize_scene_result(
        self,
        result: SceneClassificationResult,
        request: GlobalDomainRequest,
        runtime_config: SceneSelectionRuntimeConfig,
    ) -> SceneClassificationResult:
        result = self._apply_forced_agent_selections(
            result=result,
            request=request,
            scene_registry=runtime_config.scene_registry,
        )
        if not result.big_scenes:
            return self._empty_scene_result()
        return SceneClassificationResult.model_validate(
            result.model_dump(),
            context={"big_scene_to_sub_scenes": runtime_config.big_scene_to_sub_scenes},
        )

    async def select_big_scene(
        self,
        request: GlobalDomainRequest,
        runtime_config: SceneSelectionRuntimeConfig | None = None,
        history_context: str | None = None,
    ) -> tuple[BigSceneClassificationResult, LLMResponse | None]:
        query = request.query
        try:
            config = runtime_config or self.resolve_runtime_config(request)
        except ValidationError as exc:
            logger.error(f"Invalid scene selection config: {exc}")
            return BigSceneClassificationResult.model_construct(big_scenes=[]), None
        except Exception as exc:
            logger.error(f"Failed to resolve scene selection config: {exc}")
            return BigSceneClassificationResult.model_construct(big_scenes=[]), None

        if not self.has_selectable_agents(config):
            logger.info("No scene agents enabled; skipping big scene classification")
            return BigSceneClassificationResult.model_construct(big_scenes=[]), None

        resolved_history_context = (
            history_context
            if history_context is not None
            else self._build_history_context(
                list(request.history) if request.history is not None else None,
                history_turn_limit=self._history_turn_limit,
            )
        )

        prompt = self._render_template(
            self._big_scene_user_prompt_template,
            {
                "history_context": resolved_history_context,
                "query": query,
            },
        )

        logger.info(f"Big scene selection started for query: {query}")
        try:
            response = await self._llm.asimple_chat(
                system_prompt=config.big_scene_system_prompt,
                prompt=prompt,
                json_schema=self._build_big_scene_json_schema(
                    list(config.scene_registry.keys())
                ),
                schema_name="scene_classification",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Big scene classification LLM call failed: {exc}")
            return BigSceneClassificationResult.model_construct(big_scenes=[]), None

        try:
            result = BigSceneClassificationResult.model_validate_json(response.content)
            result, dropped = self._normalize_big_scene_labels(
                result, config.scene_registry.keys()
            )
            if dropped:
                logger.warning(f"Dropped invalid big_scenes: {dropped}")
            return result, response
        except ValidationError as exc:
            logger.error(
                "Big scene response validation failed: "
                f"{exc}. content={self._truncate_text(response.content)}"
            )
            return BigSceneClassificationResult.model_construct(big_scenes=[]), response
        except Exception as exc:
            logger.error(
                "Big scene response parsing failed: "
                f"{exc}. content={self._truncate_text(response.content)}"
            )
            return BigSceneClassificationResult.model_construct(big_scenes=[]), response

    async def select_scene(
        self,
        request: GlobalDomainRequest,
    ) -> SceneClassificationResult:
        outcome = await self.select_scene_two_steps(request)
        return outcome.result

    async def select_scene_two_steps(
        self,
        request: GlobalDomainRequest,
        *,
        observer: SceneSelectionObserver | None = None,
    ) -> SceneSelectionOutcome:
        """Directly route to sub-agents while preserving the legacy result shape."""
        try:
            runtime_config = self.resolve_runtime_config(request)
        except ValidationError as exc:
            logger.error(f"Invalid request scene selection config: {exc}")
            return self._empty_scene_outcome()
        except Exception as exc:
            logger.error(f"Failed to parse scene selection config: {exc}")
            return self._empty_scene_outcome()

        if not self.has_selectable_agents(runtime_config):
            logger.info("No scene agents enabled; skipping scene classification")
            return self._empty_scene_outcome()

        return await self.select_scene_direct(
            request,
            runtime_config=runtime_config,
            observer=observer,
        )

    async def select_scene_direct(
        self,
        request: GlobalDomainRequest,
        *,
        runtime_config: SceneSelectionRuntimeConfig,
        observer: SceneSelectionObserver | None = None,
    ) -> SceneSelectionOutcome:
        history_context = self._build_history_context(
            request.history,
            history_turn_limit=self._history_turn_limit,
        )
        token_usage: dict[str, int] = {}
        available_agents: list[dict[str, str]] = []
        for big_scene, payload in runtime_config.scene_registry.items():
            for agent_code, description in payload["sub_scenes"].items():
                enabled_meta = (
                    runtime_config.enabled_agent_codes or {}
                ).get(agent_code)
                available_agents.append(
                    {
                        "agent_code": agent_code,
                        "agent_name": enabled_meta.agent_name
                        if enabled_meta is not None
                        else agent_code,
                        "description": enabled_meta.agent_description
                        if enabled_meta is not None
                        else description,
                        "big_scene": big_scene,
                    }
                )

        allowed_codes = {item["agent_code"] for item in available_agents}
        code_to_big_scene = {
            item["agent_code"]: item["big_scene"] for item in available_agents
        }
        scene_selection = cast(
            SceneSelectionConfigSchema | None,
            getattr(request, "scene_selection", None),
        )
        route_prompt = (
            getattr(scene_selection, "route_prompt", None)
            or "你是 MAP Master 路由智能体。请根据用户问题直接选择最适合回答的业务智能体。"
        )
        route_llm = self._llm
        route_llm_config = getattr(scene_selection, "route_llm_config", None)
        if route_llm_config is not None:
            try:
                route_llm = LLMEngine(config=route_llm_config)
            except Exception as exc:
                logger.warning(f"Invalid route_llm_config, using default LLM: {exc}")
        user_prompt = (
            f"{history_context}\n用户问题：{request.query}\n\n"
            "可用业务智能体：\n"
            f"{available_agents}\n\n"
            "请仅输出 JSON，字段 agent_routes 为数组；每项包含 agent_code、confidence、reason。"
        )

        self._notify_stage_start(
            observer,
            "direct_sub_agent_route",
            {"query": request.query, "available_agents": available_agents},
        )
        route_start = time.perf_counter()
        response: LLMResponse | None = None
        route_items: list[dict[str, Any]] = []
        try:
            with llm_trace_context(
                state_store=getattr(observer, "state_store", None),
                state_id=getattr(observer, "state_id", None),
                request_id=(getattr(observer, "base_state", {}) or {}).get("request_id")
                if isinstance(getattr(observer, "base_state", None), dict)
                else None,
                session_id=(getattr(observer, "base_state", {}) or {}).get("session_id")
                if isinstance(getattr(observer, "base_state", None), dict)
                else None,
                staff_code=(getattr(observer, "base_state", {}) or {}).get("staff_code")
                if isinstance(getattr(observer, "base_state", None), dict)
                else None,
                agent_code="Master",
                agent_name="Master 智能体",
                component="direct_sub_agent_router",
                phase="master_route",
                call_kind="route",
            ):
                response = await route_llm.asimple_chat(
                    prompt=user_prompt,
                    system_prompt=route_prompt,
                    json_schema={
                        "type": "object",
                        "properties": {
                            "agent_routes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "agent_code": {
                                            "type": "string",
                                            "enum": sorted(allowed_codes),
                                        },
                                        "confidence": {
                                            "type": "number",
                                            "minimum": 0,
                                            "maximum": 1,
                                        },
                                        "reason": {"type": "string"},
                                    },
                                    "required": [
                                        "agent_code",
                                        "confidence",
                                        "reason",
                                    ],
                                },
                            }
                        },
                        "required": ["agent_routes"],
                    },
                    schema_name="direct_sub_agent_route",
                )
            self._merge_token_usage(token_usage, response.usage)
            parsed = self._parse_json_object(response.content)
            raw_routes = parsed.get("agent_routes") if isinstance(parsed, dict) else []
            if isinstance(raw_routes, list):
                route_items = [item for item in raw_routes if isinstance(item, dict)]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Direct sub-agent routing failed: {exc}")

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in route_items:
            code = str(item.get("agent_code") or "").strip()
            if code not in allowed_codes or code in seen:
                continue
            seen.add(code)
            selected.append(
                {
                    "agent_code": code,
                    "confidence": float(item.get("confidence") or 0.5),
                    "reason": str(item.get("reason") or "direct route"),
                }
            )

        if not selected and "General_Assistant" in allowed_codes:
            selected.append(
                {
                    "agent_code": "General_Assistant",
                    "confidence": 0.3,
                    "reason": "路由失败或置信度不足，使用通用问答兜底。",
                }
            )
        elif not selected and available_agents:
            selected.append(
                {
                    "agent_code": available_agents[0]["agent_code"],
                    "confidence": 0.2,
                    "reason": "路由失败，使用首个可用智能体兜底。",
                }
            )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in selected:
            big_scene = code_to_big_scene.get(item["agent_code"])
            if big_scene is not None:
                grouped.setdefault(big_scene, []).append(item)

        result = SceneClassificationResult.model_construct(
            big_scenes=[
                SceneItem(
                    big_scene=big_scene,
                    confidence=max(float(item["confidence"]) for item in items),
                    reason="direct sub-agent route",
                )
                for big_scene, items in grouped.items()
            ],
            sub_scenes=[
                SubSceneResult.model_construct(
                    big_scene=big_scene,
                    sub_scenes=[item["agent_code"] for item in items],
                    confidence=max(float(item["confidence"]) for item in items),
                    reason="；".join(str(item["reason"]) for item in items)[:120],
                )
                for big_scene, items in grouped.items()
            ],
        )
        route_duration = time.perf_counter() - route_start
        self._notify_stage_end(
            observer,
            "direct_sub_agent_route",
            "success",
            {
                "agent_routes": selected,
                "meta": {
                    "duration_s": route_duration,
                    "token_usage": response.usage if response else None,
                },
            },
        )
        logger.info(
            f"{request.query} -> direct sub-agent routes -> {selected}"
        )
        return SceneSelectionOutcome(
            result=self._finalize_scene_result(result, request, runtime_config),
            token_usage=token_usage,
        )

    async def select_sub_scene(
        self,
        query: str,
        big_scenes: Iterable[str],
        sub_scene_descriptions: dict[str, str] | None = None,
        scene_registry: SceneRegistrySchema | dict[str, SceneConfig] | None = None,
        big_scene_to_sub_scenes: dict[str, list[str]] | None = None,
        history_context: str = "",
        sub_scene_user_prompt_template: str | None = None,
    ) -> list[SubSceneResult]:
        results, _ = await self._select_sub_scene_with_usage(
            query=query,
            big_scenes=big_scenes,
            sub_scene_descriptions=sub_scene_descriptions,
            scene_registry=scene_registry,
            big_scene_to_sub_scenes=big_scene_to_sub_scenes,
            history_context=history_context,
            sub_scene_user_prompt_template=sub_scene_user_prompt_template,
        )
        return results

    async def select_sub_scene_with_usage(
        self,
        query: str,
        big_scenes: Iterable[str],
        sub_scene_descriptions: dict[str, str] | None = None,
        scene_registry: SceneRegistrySchema | dict[str, SceneConfig] | None = None,
        big_scene_to_sub_scenes: dict[str, list[str]] | None = None,
        history_context: str = "",
        sub_scene_user_prompt_template: str | None = None,
    ) -> tuple[list[SubSceneResult], dict[str, int]]:
        """Classify sub-scenes and return aggregated token usage from sub-scene calls."""
        return await self._select_sub_scene_with_usage(
            query=query,
            big_scenes=big_scenes,
            sub_scene_descriptions=sub_scene_descriptions,
            scene_registry=scene_registry,
            big_scene_to_sub_scenes=big_scene_to_sub_scenes,
            history_context=history_context,
            sub_scene_user_prompt_template=sub_scene_user_prompt_template,
        )

    async def _select_sub_scene_with_usage(
        self,
        query: str,
        big_scenes: Iterable[str],
        sub_scene_descriptions: dict[str, str] | None = None,
        scene_registry: SceneRegistrySchema | dict[str, SceneConfig] | None = None,
        big_scene_to_sub_scenes: dict[str, list[str]] | None = None,
        history_context: str = "",
        sub_scene_user_prompt_template: str | None = None,
    ) -> tuple[list[SubSceneResult], dict[str, int]]:
        logger.info(f"Sub-scene selection started for {query}")
        resolved_registry = normalize_scene_registry(scene_registry or SCENE_REGISTRY)
        descriptions = sub_scene_descriptions or build_sub_scene_descriptions(
            resolved_registry
        )
        user_prompt_template = (
            sub_scene_user_prompt_template or self._sub_scene_user_prompt_template
        )
        scene_map = big_scene_to_sub_scenes or build_big_scene_to_sub_scenes(
            resolved_registry
        )

        async def _select_one(
            big_scene: str,
        ) -> tuple[SubSceneResult | None, LLMResponse | None]:
            if big_scene not in descriptions:
                logger.warning(f"No sub-scene description for big_scene: {big_scene}")
                return None, None

            try:
                prompt = self._render_template(
                    user_prompt_template,
                    {
                        "history_context": history_context,
                        "query": query,
                        "big_scene": big_scene,
                        "sub_scene_descriptions": descriptions.get(big_scene, ""),
                    },
                )
            except KeyError as exc:
                logger.error(
                    f"Invalid sub_scene_user_prompt_template missing key: {exc}"
                )
                return None, None

            response: LLMResponse | None = None
            try:
                response = await self._llm.asimple_chat(
                    system_prompt=SUB_SCENE_SYSTEM_PROMPT,
                    prompt=prompt,
                    json_schema=self._build_sub_scene_json_schema(big_scene, scene_map),
                    schema_name="sub_scene_classification",
                )
                result = SubSceneResult.model_validate_json(
                    response.content,
                    context={"big_scene_to_sub_scenes": scene_map},
                )
                normalized, invalid, mapped = self._normalize_sub_scene_labels(
                    big_scene=result.big_scene,
                    sub_scenes=result.sub_scenes,
                    scene_registry=resolved_registry,
                    big_scene_to_sub_scenes=scene_map,
                )
                if mapped:
                    logger.warning(
                        f"Mapped non-standard sub-scenes for {big_scene}: {mapped}"
                    )
                if invalid:
                    logger.warning(
                        f"Dropped invalid sub-scenes for {big_scene}: {invalid}"
                    )
                return result.model_copy(update={"sub_scenes": normalized}), response
            except ValidationError as exc:
                content = response.content if response else ""
                logger.error(
                    "Sub-scene response validation failed for "
                    f"{big_scene}: {exc}. content={self._truncate_text(content)}"
                )
                return None, response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Failed to classify sub-scene for {big_scene}: {exc}")
                return None, response

        tasks = [_select_one(bs) for bs in big_scenes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cleaned: list[SubSceneResult] = []
        token_usage: dict[str, int] = {}
        for res in results:
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                logger.error(f"Sub-scene task failed: {res}")
                continue
            sub_scene_result, llm_response = res
            if sub_scene_result is not None:
                cleaned.append(sub_scene_result)
            if llm_response is not None and llm_response.usage:
                for key, value in llm_response.usage.items():
                    if isinstance(value, int):
                        token_usage[key] = token_usage.get(key, 0) + value
        return cleaned, token_usage
