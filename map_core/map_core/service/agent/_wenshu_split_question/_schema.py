from typing import Literal

from pydantic import BaseModel, Field

VERSION: Literal["v1", "v2"] = "v2"

MILVUS_DB_NAME_PREFIX = "DATA_MODEL_SYNC_"
METRIC_INFO_DRAFT_COLLECTION = "abc_metric_info_draft"
METRIC_INFO_PUBLISHED_COLLECTION = "abc_metric_info_published"

DIMENSION_INFO_DRAFT_COLLECTION = "abc_dimension_info_draft"
DIMENSION_INFO_PUBLISHED_COLLECTION = "abc_dimension_info_published"

DIMENSION_DETAIL_COLLECTION_PREFIX = "dim_"

DATA_SOURCE_ID_FIELD_NAME = "data_origin_id"
DATA_MODEL_ID_FIELD_NAME = "unique_id"
METRIC_DEFINITION_FIELD_NAME = "metric_definition"


if VERSION == "v1":
    MILVUS_DB_NAME_PREFIX = "dataorigin_"
    METRIC_INFO_DRAFT_COLLECTION = "metric"
    METRIC_INFO_PUBLISHED_COLLECTION = "metric"

    DIMENSION_INFO_DRAFT_COLLECTION = "dimension"
    DIMENSION_INFO_PUBLISHED_COLLECTION = "dimension"

    DIMENSION_DETAIL_COLLECTION_PREFIX = "dimension_"

    DATA_SOURCE_ID_FIELD_NAME = "data_origin_id"
    DATA_MODEL_ID_FIELD_NAME = "metric_unique_id"
    METRIC_DEFINITION_FIELD_NAME = "metric_meaning"

class IdentifyMetricsResponse(BaseModel):
    metrics: list[str] = Field(description="列表，包含提取出的指标的 metric_code")
    reasoning: str = Field(description="选择这些指标的理由，字数在100字以内")

class SubQuestionResponse(BaseModel):
    sub_questions: list[str] = Field(description="针对某个指标拆解出的一条或多条子问题")
