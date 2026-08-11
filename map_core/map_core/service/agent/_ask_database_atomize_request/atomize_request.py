import asyncio
import json
import re
import time
from datetime import datetime
from typing import Callable, Literal

from loguru import logger

from map_core.utils.llm_engine import LLMEngine

from ._prompts import REWRITING_PROMPT_TEMPLATE
from ._schema import AtomizeContext, DecomposedTask, PrunedSchema
from .process_schema import process_schema
from .select_data_models import select_data_models


def _parse_code_block(content: str) -> str:
    # 提取多行 markdown json block 中的内容
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()


async def atomize_question(
    context: AtomizeContext,
    pruned_schema: PrunedSchema,
    llm: LLMEngine,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[DecomposedTask]:
    """
    Rewrites and decomposes the user's question into precise sub-questions based on the pruned schema.
    Injects context.user_prompt into the prompt if provided.
    """
    start_time = time.perf_counter()

    formatted_fields = []
    for col in pruned_schema.relevant_columns:
        name = col.get("name", "")
        desc = col.get("description", "")
        ctype = col.get("type", "")
        examples = col.get("examples", [])
        examples_str = f", Examples: {examples[:3]}" if examples else ""
        formatted_fields.append(f"- {name} ({desc}) [{ctype}]{examples_str}")

    fields_str = "\n".join(formatted_fields)

    # Inject user prompt
    user_prompt_str = ""
    if context.user_prompt:
        user_prompt_str = f"[后台用户提示词]\n{context.user_prompt}\n"

    current = datetime.now().strftime("%Y 年 %m 月 %d 日")
    prompt = REWRITING_PROMPT_TEMPLATE.format(
        current=current,
        user_prompt=user_prompt_str,
        question=context.question,
        fields=fields_str
    )

    try:
        if context.system_prompt:
            response = await llm.ainvoke([{"role": "system", "content": context.system_prompt}, {"role": "user", "content": prompt}])
        else:
            response = await llm.ainvoke([{"role": "user", "content": prompt}])
        if usage_callback is not None:
            usage_callback(response.usage)
        content = response.content
        parsed_data = json.loads(_parse_code_block(content))

        sub_questions = parsed_data.get("sub_questions", [])
        if not isinstance(sub_questions, list) or len(sub_questions) == 0:
            raise ValueError("LLM returned empty or invalid sub_questions")

        decomposed_tasks = []
        for sub_q in sub_questions:
            if isinstance(sub_q, str) and sub_q.strip():
                decomposed_tasks.append(
                    DecomposedTask(
                        sub_question=sub_q.strip(),
                        pruned_schema=pruned_schema
                    )
                )

        if not decomposed_tasks:
            raise ValueError("No valid sub_questions parsed")

        elapsed = time.perf_counter() - start_time
        logger.info(
            f"Rewriter produced {len(decomposed_tasks)} sub-questions in {elapsed:.2f}s, "
            f"sub-questions: {[t.sub_question for t in decomposed_tasks]}"
        )
        return decomposed_tasks

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.error(f"Rewriter failed after {elapsed:.2f}s: {e}. Falling back to original question.")
        return [
            DecomposedTask(
                sub_question=f"Answer the following directly: {context.question}",
                pruned_schema=pruned_schema
            )
        ]


async def process_atomize_pipeline(
    context: AtomizeContext,
    llm: LLMEngine,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[DecomposedTask]:
    """
    Main orchestrator for the Atomize Request pipeline.
    Steps:
      1. select_data_models
      2. process_schema (for each selected model)
      3. atomize_question (for each pruned schema)
    """
    logger.info("Starting atomize pipeline execution.")
    start_time = time.perf_counter()

    # Step 1: Selection Phase
    selected_models = await select_data_models(context, llm, usage_callback)
    if not selected_models:
        logger.warning("Pipeline terminated early: Selection returned 0 models.")
        return []

    # Step 2 & 3: Process each valid model
    async def _process_single_data_model(data_model):
        logger.info(f"Processing Model: {data_model.unique_id} ({data_model.data_model_name})")
        pruned_schema = await process_schema(context, data_model, llm, usage_callback)
        return await atomize_question(context, pruned_schema, llm, usage_callback)

    tasks = [_process_single_data_model(model) for model in selected_models]
    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    final_tasks: list[DecomposedTask] = []
    for result in results_list:
        if isinstance(result, BaseException):
            logger.error(f"Error processing model: {result}")
        else:
            final_tasks.extend(result)

    total_elapsed = time.perf_counter() - start_time
    logger.info(f"Pipeline completed in {total_elapsed:.2f}s. Total sub-tasks generated: {len(final_tasks)}")

    return final_tasks