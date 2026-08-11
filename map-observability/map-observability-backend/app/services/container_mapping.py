from __future__ import annotations

MAIN_FLOW_CONTAINERS = {
    "map_core-dev",
    "map_core-test",
    "map_core-preprod",
}

MAIN_FLOW_CONTAINER_ENV_MAP = {
    "map_core-dev": "dev",
    "map_core-test": "test",
    "map_core-preprod": "preprod",
}

ENV_MAIN_FLOW_CONTAINER_MAP = {
    env: container for container, env in MAIN_FLOW_CONTAINER_ENV_MAP.items()
}

CBB_CONTAINER_TOOL_MAP = {
    "cbb-text-to-metrics-dev": "wenshu_agent",
    "cbb-text-to-metrics-test": "wenshu_agent",
    "cbb-text-to-metrics-preprod": "wenshu_agent",
    "cbb-text-to-sql-dev": "ask_database_agent",
    "cbb-text-to-sql-test": "ask_database_agent",
    "cbb-text-to-sql-preprod": "ask_database_agent",
    "cbb-text-to-ngql-dev": "efficiency_pi_agent",
    "cbb-text-to-ngql-test": "efficiency_pi_agent",
    "cbb-text-to-ngql-preprod": "efficiency_pi_agent",
    "cbb-kb-analyze-dev": "search_mounted_kb_agent",
    "cbb-kb-analyze-test": "search_mounted_kb_agent",
    "cbb-kb-analyze-preprod": "search_mounted_kb_agent",
    "cbb-kb-analyze-ubddev201": "search_mounted_kb_agent",
}

ALL_SUPPORTED_CONTAINERS = MAIN_FLOW_CONTAINERS.union(CBB_CONTAINER_TOOL_MAP.keys())

SPECIAL_CONTAINER_ENV_MAP = {
    "cbb-kb-analyze-ubddev201": "preprod",
}


def _infer_env_from_container_name(container_name: str) -> str | None:
    special_env = SPECIAL_CONTAINER_ENV_MAP.get(container_name)
    if special_env:
        return special_env
    if container_name in MAIN_FLOW_CONTAINER_ENV_MAP:
        return MAIN_FLOW_CONTAINER_ENV_MAP[container_name]
    if container_name.endswith("-preprod"):
        return "preprod"
    if container_name.endswith("-test"):
        return "test"
    if container_name.endswith("-dev"):
        return "dev"
    return None


TOOL_TO_CBB_CONTAINER: dict[str, dict[str, str]] = {}
for container_name, tool_name in CBB_CONTAINER_TOOL_MAP.items():
    env = _infer_env_from_container_name(container_name)
    if env is None:
        continue
    mapping = TOOL_TO_CBB_CONTAINER.setdefault(tool_name, {})
    mapping[env] = container_name


def assert_container_supported(container: str) -> str:
    normalized = str(container or "").strip()
    if normalized not in ALL_SUPPORTED_CONTAINERS:
        raise ValueError(f"container must be one of {sorted(ALL_SUPPORTED_CONTAINERS)}")
    return normalized


def is_cbb_container(container: str) -> bool:
    normalized = str(container or "").strip()
    return normalized in CBB_CONTAINER_TOOL_MAP


def mapped_tool_for_container(container: str) -> str | None:
    normalized = str(container or "").strip()
    return CBB_CONTAINER_TOOL_MAP.get(normalized)


def infer_cbb_container_by_tool(tool: str | None, base_container: str) -> str | None:
    normalized_tool = str(tool or "").strip()
    if not normalized_tool:
        return None

    pair = TOOL_TO_CBB_CONTAINER.get(normalized_tool)
    if not pair:
        return None

    main_flow_container = infer_main_flow_container(base_container)
    env = MAIN_FLOW_CONTAINER_ENV_MAP[main_flow_container]
    return pair.get(env) or pair.get("dev") or pair.get("test") or pair.get("preprod")


def enforce_container_tool(container: str, tool: str | None) -> str | None:
    normalized_container = assert_container_supported(container)
    normalized_tool = str(tool or "").strip() or None

    mapped_tool = mapped_tool_for_container(normalized_container)
    if mapped_tool is None:
        return normalized_tool

    if normalized_tool and normalized_tool != mapped_tool:
        raise ValueError(
            f"container={normalized_container} enforces tool={mapped_tool}, "
            f"but received tool={normalized_tool}"
        )
    return mapped_tool


def infer_main_flow_container(container: str) -> str:
    normalized = assert_container_supported(container)
    if normalized in MAIN_FLOW_CONTAINERS:
        return normalized

    env = _infer_env_from_container_name(normalized)
    if env and env in ENV_MAIN_FLOW_CONTAINER_MAP:
        return ENV_MAIN_FLOW_CONTAINER_MAP[env]

    raise ValueError(f"cannot infer main flow container for {normalized}")
