import json
import re
import time
from typing import Callable

from loguru import logger

from map_core.utils.llm_engine import LLMEngine

from ....utils.milvus import get_milvus_client, hybrid_search_with_bm25
from ....utils.model_factory import aembed_text
from ._prompts import PRUNING_PROMPT_TEMPLATE
from ._schema import AtomizeContext, FilteredDataModel, PrunedSchema

COLUMN_COLLECTION_PREFIX = "col_"
COLUMN_COLLECTION_SUFFIX = "_draft"
TOP_K_COLUMNS = 20
LLM_PRUNING_THRESHOLD = 50
COLUMN_OUTPUT_FIELDS = [
    "column_name",
    "column_type",
    "column_description",
    "column_examples_text",
]


def _parse_code_block(content: str) -> str:
    # 提取多行 markdown json block 中的内容
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()


async def process_schema(
    context: AtomizeContext,
    data_model: FilteredDataModel,
    llm: LLMEngine,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> PrunedSchema:
    """
    Prunes the wide table schema columns to only the most relevant ones.
    Uses Hybrid Search (COSINE + BM25) and falls back to LLM pruning.
    """
    start_time = time.perf_counter()

    all_columns = data_model.columns

    if len(all_columns) <= LLM_PRUNING_THRESHOLD:
        logger.info(f"Skipping pruning. Column count {len(all_columns)} <= {LLM_PRUNING_THRESHOLD}")
        return PrunedSchema(
            model_id=data_model.unique_id,
            data_source_id=data_model.data_source_id,
            data_model_name=data_model.data_model_name,
            data_model_description=data_model.data_model_description,
            wide_table_sql=data_model.wide_table_sql,
            relevant_columns=all_columns,
        )

    try:
        pruned = await _hybrid_search_prune(context, data_model)
        if pruned is not None:
            elapsed = time.perf_counter() - start_time
            logger.info(
                f"Hybrid search pruned {len(all_columns)} → "
                f"{len(pruned.relevant_columns)} columns in {elapsed:.2f}s"
            )
            return pruned
    except Exception as e:
        logger.warning(f"Hybrid search pruning failed: {e}. Falling back to LLM pruning.")

    try:
        pruned = await _llm_prune(context, data_model, llm, usage_callback)
        elapsed = time.perf_counter() - start_time
        logger.info(
            f"LLM pruned {len(all_columns)} → "
            f"{len(pruned.relevant_columns)} columns in {elapsed:.2f}s"
        )
        return pruned
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.error(
            f"LLM pruning also failed after {elapsed:.2f}s: {e}. "
            f"Returning all {len(all_columns)} columns."
        )
        return PrunedSchema(
            model_id=data_model.unique_id,
            data_source_id=data_model.data_source_id,
            data_model_name=data_model.data_model_name,
            data_model_description=data_model.data_model_description,
            wide_table_sql=data_model.wide_table_sql,
            relevant_columns=all_columns
        )


async def _hybrid_search_prune(
    context: AtomizeContext,
    data_model: FilteredDataModel,
) -> PrunedSchema | None:
    db_name = f"DATA_MODEL_SYNC_{context.agent_id}"
    milvus_client = await get_milvus_client(db_name=db_name)
    col_collection = f"{COLUMN_COLLECTION_PREFIX}{data_model.unique_id}{COLUMN_COLLECTION_SUFFIX}"

    if not await milvus_client.has_collection(col_collection):
        logger.warning(f"Column collection '{col_collection}' not found. Skipping hybrid search.")
        return None

    question_embedding = await aembed_text(text=context.question)

    hits = await hybrid_search_with_bm25(
        question=context.question,
        question_embedding=question_embedding,
        client=milvus_client,
        collection_name=col_collection,
        index_type="hnsw",
        anns_fields=["column_description_embedding", "column_description_sparse_vector"],
        search_params=[{"metric_type": "COSINE", "nprobe": 50}, {}],
        weights=[0.6, 0.4],
        output_fields=COLUMN_OUTPUT_FIELDS,
        top_k=TOP_K_COLUMNS,
    )

    if not hits:
        logger.warning(f"Hybrid search returned 0 hits for '{col_collection}'.")
        return None

    relevant_columns = []
    retrieved_names = set()
    allowed_columns = {c.get("name") for c in data_model.columns}
    logger.debug(f"Allowed columns from Selection: {allowed_columns}")

    for hit in hits:
        entity = hit.entity if hasattr(hit, "entity") else hit.get("entity", hit)
        col_name = entity.get("column_name", "")
        col_desc = entity.get("column_description", "")
        col_type = entity.get("column_type", "")
        examples_text = entity.get("column_examples_text", "")
        examples = [e for e in examples_text.split(" ") if e] if examples_text else []

        alias = col_desc or col_name

        if col_name in retrieved_names:
            continue

        if col_name not in allowed_columns:
            logger.debug(f"Hybrid search hit '{col_name}' ({alias}) is NOT in allowed_columns, discarding.")
            continue

        retrieved_names.add(col_name)

        relevant_columns.append({
            "name": col_name,
            "description": alias,
            "type": col_type,
            "examples": examples,
        })

    if not relevant_columns:
        logger.warning("No columns parsed from hybrid search hits.")
        return None

    return PrunedSchema(
        model_id=data_model.unique_id,
        data_source_id=data_model.data_source_id,
        data_model_name=data_model.data_model_name,
        data_model_description=data_model.data_model_description,
        wide_table_sql=data_model.wide_table_sql,
        relevant_columns=relevant_columns,
    )


async def _llm_prune(
    context: AtomizeContext,
    data_model: FilteredDataModel,
    llm: LLMEngine,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> PrunedSchema:
    all_columns = data_model.columns

    formatted_fields = []
    for col in all_columns:
        name = col.get("name", "")
        desc = col.get("description", "")
        ctype = col.get("type", "")
        formatted_fields.append(f"- {name} ({desc}) [{ctype}]")
    fields_str = "\n".join(formatted_fields)

    prompt = PRUNING_PROMPT_TEMPLATE.format(
        question=context.question,
        fields=fields_str,
    )

    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        if usage_callback is not None:
            usage_callback(response.usage)
        content = response.content
        parsed_data = json.loads(_parse_code_block(content))
    except Exception as e:
        logger.error(f"LLM pruning failed: {e}")
        raise

    selected_names = parsed_data.get("selected_column_names", [])
    if not isinstance(selected_names, list) or len(selected_names) == 0:
        raise ValueError("LLM returned empty or invalid selected_column_names")

    selected_set = set(selected_names)
    relevant_columns = [col for col in all_columns if col.get("name") in selected_set]

    if not relevant_columns:
        logger.warning("LLM column name mismatch; returning all columns.")
        relevant_columns = all_columns

    return PrunedSchema(
        model_id=data_model.unique_id,
        data_source_id=data_model.data_source_id,
        data_model_name=data_model.data_model_name,
        data_model_description=data_model.data_model_description,
        wide_table_sql=data_model.wide_table_sql,
        relevant_columns=relevant_columns,
    )