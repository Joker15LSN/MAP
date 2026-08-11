import enum
from typing import Any, Dict, List, Optional, Union

import httpx
from loguru import logger
from pydantic import BaseModel, Field


class SourceName(enum.Enum):
    REPORT_MARKET = 'REPORT_MARKET'
    KMS = 'KMS'
    OA = 'OA'
    SEP = 'SEP'


class RetrieveItemSchema(BaseModel):
    title: str
    snippet: str
    contents: Optional[List[str]] = None
    score: float
    source: str
    url: Optional[str] = None
    url_mobile: Optional[str] = None
    rerank_score: float = -1.0
    create_time: Optional[str] = None
    create_timestamp: Optional[int] = None
    favicon_url: Optional[str] = None
    website_name: Optional[str] = None
    kb_code: Optional[str] = None
    kb_name: Optional[str] = None
    kb_file_id: Optional[str] = None
    kb_file_type: Optional[str] = None
    kb_qa_tag: Optional[str] = Field(default=None)
    kb_convert_pdf_file_id: Optional[str] = None
    kb_file_labels: Optional[List[str]] = None


class RetrieveResultMetaSchema(BaseModel):
    count: int = 0
    error_msgs: Dict[str, List[str]] = Field(default_factory=dict)


class RetrieveResponseDataSchema(BaseModel):
    items: List[RetrieveItemSchema] = Field(default_factory=list)
    meta: RetrieveResultMetaSchema = Field(
        default_factory=lambda: RetrieveResultMetaSchema(count=0, error_msgs={})
    )


class RetrieveResponseSchema(BaseModel):
    request_id: str
    data: Optional[RetrieveResponseDataSchema] = None
    success: bool = False
    message: Optional[str] = None


async def fetch_aggr_retrieve(
    req_id: str,
    api_url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: Optional[float] = None
) -> RetrieveResponseSchema:
    async with httpx.AsyncClient(timeout=(timeout or 60) * 0.8) as client:
        try:
            response = await client.post(
                api_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            res_obj = response.json()
            try:
                res =  RetrieveResponseSchema(**res_obj)
            except Exception as e:
                logger.error(f'validate enterprise kb search fails: {e}')
                res = RetrieveResponseSchema(request_id=req_id)
            return res
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else "unknown"
            body = exc.response.text if exc.response else ""
            logger.exception(
                "zhiwen_agent API call failed with HTTP status "
                f"{status}. Body: {body}"
            )
            raise
        except httpx.RequestError:
            logger.exception("zhiwen_agent API call failed (request error)")
            raise

async def fetch_aggr_retrieve_as_dict(
    req_id: str,
    api_url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: Optional[float] = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=(timeout or 60) * 0.8) as client:
            try:
                response = await client.post(
                    api_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else "unknown"
                body = exc.response.text if exc.response else ""
                logger.exception(
                    "zhiwen_agent API call failed with HTTP status "
                    f"{status}. Body: {body}"
                )
                raise
            except httpx.RequestError:
                logger.exception("zhiwen_agent API call failed (request error)")
                raise

        return response.json()

