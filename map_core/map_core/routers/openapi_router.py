from typing import Any

from fastapi import APIRouter, Request

openapi_router = APIRouter()
YAPI_MAP_KEY_PLACEHOLDER = "<string_key>"


def _is_null_schema(candidate: Any) -> bool:
    return isinstance(candidate, dict) and candidate.get("type") == "null"


def _merge_nullable_union(schema_node: dict[str, Any]) -> dict[str, Any]:
    """Convert nullable unions to OpenAPI 3.0-friendly schema for YApi."""
    for union_key in ("anyOf", "oneOf"):
        variants = schema_node.get(union_key)
        if not isinstance(variants, list):
            continue

        non_null_variants = [item for item in variants if not _is_null_schema(item)]
        has_null_variant = len(non_null_variants) != len(variants)
        if not has_null_variant or len(non_null_variants) != 1:
            continue

        base_schema = non_null_variants[0]
        if not isinstance(base_schema, dict):
            continue

        merged: dict[str, Any] = {
            key: value for key, value in schema_node.items() if key != union_key
        }
        for key, value in base_schema.items():
            merged.setdefault(key, value)
        merged["nullable"] = True
        return merged

    return schema_node


def _materialize_additional_properties(schema_node: dict[str, Any]) -> dict[str, Any]:
    """Expose map value schemas as a representative property for YApi rendering."""
    if schema_node.get("type") != "object":
        return schema_node

    value_schema = schema_node.get("additionalProperties")
    if not isinstance(value_schema, dict):
        return schema_node

    existing_properties = schema_node.get("properties")
    if existing_properties is not None and not isinstance(existing_properties, dict):
        return schema_node

    properties = dict(existing_properties or {})
    properties.setdefault(YAPI_MAP_KEY_PLACEHOLDER, value_schema)

    description = schema_node.get("description")
    map_hint = (
        f'YApi view note: dynamic object keys are represented by '
        f'"{YAPI_MAP_KEY_PLACEHOLDER}".'
    )

    normalized = dict(schema_node)
    normalized.pop("additionalProperties", None)
    normalized["properties"] = properties
    normalized["description"] = (
        f"{description}\n\n{map_hint}" if isinstance(description, str) and description else map_hint
    )
    return normalized


def build_yapi_compatible_openapi(raw_schema: Any) -> Any:
    """Recursively normalize OpenAPI 3.1 schema so YApi keeps object details."""
    if isinstance(raw_schema, list):
        return [build_yapi_compatible_openapi(item) for item in raw_schema]

    if isinstance(raw_schema, dict):
        normalized = {
            key: build_yapi_compatible_openapi(value)
            for key, value in raw_schema.items()
        }
        normalized = _merge_nullable_union(normalized)
        return _materialize_additional_properties(normalized)

    return raw_schema


@openapi_router.get("/openapi-yapi.json", include_in_schema=False)
async def openapi_yapi_json(request: Request) -> dict[str, Any]:
    return build_yapi_compatible_openapi(request.app.openapi())
