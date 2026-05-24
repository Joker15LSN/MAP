from typing import Any

from pydantic import BaseModel, Field

WIDE_TABLE_MODEL_DRAFT_COLLECTION = "abc_wide_table_model_draft"
WIDE_TABLE_MODEL_PUBLISHED_COLLECTION = "abc_wide_table_model_published"

class AtomizeContext(BaseModel):
    """Context for the atomize request pipeline."""
    request_id: str | None = None
    user_id: int
    agent_id: int
    query_mode: str = "publish"
    environment_url: str | None = None
    authorization_token: str | None = None
    question: str
    selected_data_model_ids: list[int] = Field(default_factory=list)
    system_prompt: str | None = None
    user_prompt: str | None = None


class FilteredDataModel(BaseModel):
    unique_id: str
    data_source_id: str
    data_model_name: str
    data_model_description: str
    wide_table_sql: str
    columns: list[dict[str, Any]]


class PrunedSchema(BaseModel):
    model_id: str
    data_source_id: str
    data_model_name: str
    data_model_description: str
    wide_table_sql: str
    relevant_columns: list[dict[str, Any]]


class DecomposedTask(BaseModel):
    sub_question: str
    pruned_schema: PrunedSchema
