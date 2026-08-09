import asyncio
import json
import re
import time
from typing import Callable

from loguru import logger

from map_core.utils.llm_engine import LLMEngine

from ....utils.milvus import get_milvus_client
from ....utils.model_factory import aembed_text
from ._prompts import SELECTING_PROMPT_TEMPLATE
from ._schema import (
    WIDE_TABLE_MODEL_DRAFT_COLLECTION,
    WIDE_TABLE_MODEL_PUBLISHED_COLLECTION,
    AtomizeContext,
    FilteredDataModel,
)

WIDE_TABLE_MODEL_COLLECTION_NAME = "abc_wide_table_model_draft"

MILVUS_OUTPUT_FIELDS = [
    "unique_id",
    "data_origin_id",
    "data_model_name",
    "data_model_description",
    "wide_table_sql",
    "schema_summary",
]

EMBEDDING_TOP_K = 10


def _parse_code_block(content: str) -> str:
    # 提取多行 markdown json block 中的内容
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()


async def select_data_models(
    context: AtomizeContext,
    llm: LLMEngine,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[FilteredDataModel]:
    """
    Selects relevant wide-table models for the given user question.
    Injects context.system_prompt into the prompt if provided.
    """
    start_time = time.perf_counter()

    if not context.selected_data_model_ids:
        logger.warning("No selected_data_model_ids provided. Returning empty selection.")
        return []

    db_name = f"DATA_MODEL_SYNC_{context.agent_id}"
    try:
        milvus_client = await get_milvus_client(db_name=db_name)
    except Exception as e:
        logger.error(f"Failed to get milvus client for db {db_name}: {e}")
        return []

    ids_str = ", ".join([f"'{model_id}'" for model_id in context.selected_data_model_ids])
    filter_expr = f"unique_id in [{ids_str}]"

    candidates = await _embedding_search(
        context=context,
        milvus_client=milvus_client,
        filter_expr=filter_expr,
    )

    if not candidates:
        logger.warning("Embedding search returned 0 candidates. Aborting.")
        return []

    embed_elapsed = time.perf_counter() - start_time
    logger.info(
        f"Embedding search returned {len(candidates)} candidates "
        f"in {embed_elapsed:.2f}s"
    )

    return await _llm_select(
        context=context,
        candidates=candidates,
        start_time=start_time,
        llm=llm,
        usage_callback=usage_callback,
    )


async def _embedding_search(
    context: AtomizeContext,
    milvus_client,
    filter_expr: str,
) -> list[FilteredDataModel]:
    logger.info("Embedding question for vector search...")

    try:
        question_embedding = await aembed_text(text=context.question)
    except Exception as e:
        logger.warning(f"Failed to embed question: {e}. Falling back to plain query.")
        return await _plain_query(context, milvus_client, filter_expr)

    logger.info(
        f"Running ANN search on {WIDE_TABLE_MODEL_COLLECTION_NAME} "
        f"with filter: {filter_expr}"
    )

    try:
        search_results = await milvus_client.search(
            collection_name=WIDE_TABLE_MODEL_COLLECTION_NAME,
            data=[question_embedding],
            anns_field="embedding",
            search_params={"metric_type": "COSINE", "params": {"nprobe": 128}},
            filter=filter_expr,
            limit=EMBEDDING_TOP_K,
            output_fields=MILVUS_OUTPUT_FIELDS,
        )
        hits = search_results[0] if search_results else []
    except Exception as e:
        logger.warning(
            f"ANN search failed: {e}. Falling back to plain query."
        )
        return await _plain_query(context, milvus_client, filter_expr)

    return await _parse_hits(hits, context)


async def _plain_query(
    context: AtomizeContext,
    milvus_client,
    filter_expr: str,
) -> list[FilteredDataModel]:
    logger.info(f"Running plain query with filter: {filter_expr}")
    try:
        results = await milvus_client.query(
            collection_name=WIDE_TABLE_MODEL_COLLECTION_NAME,
            filter=filter_expr,
            output_fields=MILVUS_OUTPUT_FIELDS,
        )
    except Exception as e:
        logger.error(f"Plain query failed: {e}")
        return []

    return await _parse_records(results, context)


async def _parse_hits(hits, context: AtomizeContext) -> list[FilteredDataModel]:
    entities = [hit.entity if hasattr(hit, "entity") else hit.get("entity", hit) for hit in hits]
    tasks = [_build_candidate(entity, context) for entity in entities]
    results = await asyncio.gather(*tasks)
    return [c for c in results if c is not None]


async def _parse_records(records: list[dict], context: AtomizeContext) -> list[FilteredDataModel]:
    tasks = [_build_candidate(record, context) for record in records]
    results = await asyncio.gather(*tasks)
    return [c for c in results if c is not None]


async def _build_candidate(raw: dict, context: AtomizeContext) -> FilteredDataModel | None:
    unique_id = raw.get("unique_id", "")
    data_source_id = raw.get("data_origin_id", "")
    data_model_name = raw.get("data_model_name", "")
    data_model_description = raw.get("data_model_description", "")
    wide_table_sql = raw.get("wide_table_sql", "")
    schema_summary_str = raw.get("schema_summary", "{}")

    try:
        schema_data = json.loads(schema_summary_str)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse schema_summary for {unique_id}")
        schema_data = {}

    raw_fields = schema_data.get("fields", [])

    transformed_fields = []
    for f in raw_fields:
        field_name = f.get("name", "")
        chinese_alias = f.get("description", field_name)

        transformed_fields.append({
            "name": field_name,
            "description": chinese_alias,
            "type": f.get("type", ""),
            "examples": f.get("examples", []),
        })

    if not transformed_fields:
        logger.warning(f"No columns were available for {unique_id}!")

    return FilteredDataModel(
        unique_id=unique_id,
        data_source_id=data_source_id,
        data_model_name=data_model_name,
        data_model_description=data_model_description,
        wide_table_sql=wide_table_sql,
        columns=transformed_fields,
    )


async def _llm_select(
    context: AtomizeContext,
    candidates: list[FilteredDataModel],
    start_time: float,
    llm: LLMEngine,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[FilteredDataModel]:
    formatted_candidates = []
    for c in candidates:
        col_preview = [
            f"{col.get('name')} [{col.get('description', '')}]"
            for col in c.columns[:1000]
        ]
        has_more = "..." if len(c.columns) > 100 else ""
        desc = (
            f"ID: {c.unique_id}\n"
            f"Name: {c.data_model_name}\n"
            f"Description: {c.data_model_description}\n"
            f"Preview cols: {', '.join(col_preview)}{has_more}"
        )
        formatted_candidates.append(desc)

    candidates_str = "\n\n".join(formatted_candidates)

    # Inject system prompt
    system_prompt_str = ""
    if context.system_prompt:
        system_prompt_str = f"[后台系统提示词]\n{context.system_prompt}\n"

    prompt = SELECTING_PROMPT_TEMPLATE.format(
        system_prompt=system_prompt_str,
        question=context.question,
        candidates=candidates_str,
    )

    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        if usage_callback is not None:
            usage_callback(response.usage)
        content = response.content
        parsed_data = json.loads(_parse_code_block(content))

        selected_ids = parsed_data.get("selected_ids", [])
        if not isinstance(selected_ids, list):
            selected_ids = []

        final_selection = [c for c in candidates if c.unique_id in selected_ids]

        elapsed = time.perf_counter() - start_time
        logger.info(
            f"Selection Agent finished in {elapsed:.2f}s, "
            f"selected {len(final_selection)}/{len(candidates)} candidates."
        )
        return final_selection

    except Exception as e:
        logger.error(
            f"LLM selection failed: {e}. "
            f"Defaulting to all embedding-ranked candidates."
        )
        return candidates