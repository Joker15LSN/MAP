
import asyncio
import typing
from typing import Any

from loguru import logger as lgr
from pydantic import BaseModel
from pymilvus import (
    AnnSearchRequest,
    AsyncMilvusClient,
    DataType,
    Hit,
    MilvusClient,
    WeightedRanker,
)
from pymilvus.client.constants import ConsistencyLevel

# from ..._config import cfg
from ...config import METRIC_MILVUS_URI as MILVUS_URI
from ._client import _milvus_clients

_init_locks: dict[str, asyncio.Lock] = {}


async def init_milvus_client(db_name: str = "default") -> None:
    """
    初始化指定数据库的 Milvus 客户端，并缓存到 _milvus_clients。
    如果已存在，则跳过（幂等）。
    """
    if db_name in _milvus_clients:
        return  # 已初始化，直接返回

    client = AsyncMilvusClient(uri=MILVUS_URI, db_name=db_name)
    await client.list_collections()  # 验证连接和数据库有效性
    _milvus_clients[db_name] = client
    print(f"✅ Milvus client initialized for DB: {db_name}")


async def get_milvus_client(db_name: str = "default") -> AsyncMilvusClient:
    """
    获取已初始化的 Milvus 客户端。
    要求：必须先调用 init_milvus(db_name)。
    """
    if db_name in _milvus_clients:
        return _milvus_clients[db_name]

    if db_name not in _init_locks:
        _init_locks[db_name] = asyncio.Lock()

    async with _init_locks[db_name]:
        if db_name in _milvus_clients:  # double-check
            return _milvus_clients[db_name]
        await init_milvus_client(db_name)
        return _milvus_clients[db_name]


async def hybrid_search_with_bm25(
    question: str,
    question_embedding: list[float],
    client: AsyncMilvusClient,
    collection_name: str,
    index_type: str = "hnsw",
    anns_fields: list[str] = ["embedding", "sparse_vector"],
    search_params: list[dict] = [{"metric_type": "COSINE", "ef": 50}, {}],
    weights: list[float] = [0.6, 0.4],
    output_fields: list[str] = ["*"],
    top_k: int = 32,
) -> list[Hit]:
    assert len(anns_fields) == len(search_params) == len(weights) == 2, "anns_fields, search_params, and weights must have length 2"

    if index_type == "hnsw":
        search_param_hnsw: dict[str, Any] = {
            "data": [question_embedding],
            "anns_field": anns_fields[0],
            "param": search_params[0],
            "limit": 32,
        }
        search_param_bm25: dict[str, Any] = {
            "data": [question],
            "anns_field": anns_fields[1],
            "param": search_params[1],
            "limit": 32,
        }
        reqs = [
            AnnSearchRequest(**params)
            for params in [search_param_hnsw, search_param_bm25]
        ]
        ranker = WeightedRanker(0.6, 0.4)

        try:
            results = await client.hybrid_search(
                collection_name=collection_name,
                reqs=reqs,
                ranker=ranker,
                limit=top_k,
                output_fields=output_fields,
                consistency_level=ConsistencyLevel.Strong,
            )
            return results[0] if results else []  # type: ignore
        except Exception as e:
            lgr.error(f"Hybrid search failed: {e}")
            raise

    else:
        raise ValueError(f"Unsupported index type for `hybrid_search_with_bm25`: {index_type}")


async def create_collection_with_index(
    client: AsyncMilvusClient,
    collection_name: str,
    schema_model: type[BaseModel] | BaseModel,
    vector_fields: list[str],
    index_params_list: list[dict[str, Any]],
    pk_field: str | None = None,
    auto_id: bool = False,
    enable_dynamic_field: bool = False,
    varchar_default_max_length: int = 10000,
    vector_default_dimension: int = 768,
    drop_old: bool = False,
) -> None:
    """Create a Milvus collection with schema derived from a Pydantic model.

    The schema is built automatically by inspecting ``schema_model``'s fields and
    mapping Python/Pydantic annotations to Milvus ``DataType`` values:

    * ``str`` / ``Literal[...]`` → ``VARCHAR``
    * ``int`` → ``INT64``
    * ``float`` → ``FLOAT``
    * ``list[str]`` → ``ARRAY`` (element type: ``VARCHAR``)
    * ``list[float]`` in ``vector_fields`` → ``FLOAT_VECTOR``

    .. warning::
        ``list[float]`` fields that are **not** listed in ``vector_fields`` are
        **silently skipped** and will not be added to the collection schema.
        Always include every embedding field name in ``vector_fields``.

    Args:
        client: Async Milvus client instance.
        collection_name: Name of the Milvus collection to create.
        schema_model: Pydantic model class (or instance) whose fields define the schema.
        vector_fields: Field names to treat as ``FLOAT_VECTOR``.
        index_params_list: List of index param dicts passed directly to
            ``IndexParams.add_index()``.
        pk_field: Name of the primary-key field.
            If ``auto_id=False``, the field **must** exist in ``schema_model``.
            If ``auto_id=True``, the field **must not** exist in ``schema_model``
            (an ``INT64`` auto-id field is added automatically).
        auto_id: Whether Milvus should generate the primary key automatically.
        enable_dynamic_field: Enable dynamic (schemaless) extra fields.
        varchar_default_max_length: Default ``max_length`` for VARCHAR fields.
        vector_default_dimension: Default ``dim`` for FLOAT_VECTOR fields.
        drop_old: If the collection already exists, drop it before recreating.
            When ``False`` (default), the function returns immediately if the
            collection already exists.
    """

    if await client.has_collection(collection_name):
        if drop_old:
            await client.drop_collection(collection_name)
        else:
            return

    # Extract fields and docstring depending on whether model_schema is a class or instance
    model_class = schema_model if isinstance(schema_model, type) else type(schema_model)
    fields = model_class.model_fields
    model_docstring = model_class.__doc__ or ""

    _schema = MilvusClient.create_schema(
        auto_id=auto_id,
        enable_dynamic_field=enable_dynamic_field,
        description=model_docstring,
    )

    if auto_id and pk_field not in fields:
        _schema.add_field(
            field_name=pk_field,
            datatype=DataType.INT64,
            is_primary=True,
            auto_id=True,
            description="auto generated id"
        )
    elif auto_id and pk_field in fields:
        raise ValueError("auto_id is True, but pk_field is in fields")
    elif not auto_id and pk_field not in fields:
        raise ValueError("auto_id is False, but pk_field is not in fields")

    indexed_fields = {p.get("field_name") for p in index_params_list}

    for field_name, field_info in fields.items():
        is_pk = (field_name == pk_field)
        is_vector = (field_name in vector_fields)
        is_indexed = (field_name in indexed_fields)

        ann = field_info.annotation
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)

        datatype = None
        max_length = None
        dim = None
        element_type = None
        max_capacity = None

        if is_vector:
            datatype = DataType.FLOAT_VECTOR
            dim = vector_default_dimension
        elif ann is str or origin is typing.Literal:
            datatype = DataType.VARCHAR
            max_length = varchar_default_max_length
        elif ann is int:
            datatype = DataType.INT64
        elif ann is float:
            datatype = DataType.FLOAT
        elif origin is list and args and args[0] is str:
            datatype = DataType.ARRAY
            element_type = DataType.VARCHAR
            max_length = varchar_default_max_length
            max_capacity = 1000
        else:
            if is_pk or is_indexed:
                datatype = DataType.VARCHAR
                max_length = varchar_default_max_length

        if datatype is not None:
            kwargs = {
                "field_name": field_name,
                "datatype": datatype,
                "description": field_info.description or ""
            }
            if is_pk:
                kwargs["is_primary"] = True
                if auto_id:
                    kwargs["auto_id"] = True
            if max_length is not None:
                kwargs["max_length"] = max_length
            if dim is not None:
                kwargs["dim"] = dim
            if element_type is not None:
                kwargs["element_type"] = element_type
                kwargs["max_capacity"] = max_capacity

            _schema.add_field(**kwargs)

    _index_params = MilvusClient.prepare_index_params()
    for idx_param in index_params_list:
        _index_params.add_index(**idx_param)

    await client.create_collection(
        collection_name=collection_name,
        schema=_schema,
        index_params=_index_params,
    )


async def ensure_scalar_collection(
    client: AsyncMilvusClient,
    collection_name: str,
    field_definitions: list[dict[str, Any]],
    pk_field: str,
    varchar_max_length: int = 10000,
) -> None:
    """Create a Milvus collection with only scalar (VARCHAR / INT64) fields.

    Milvus requires at least one vector field per collection. This function
    satisfies that constraint by injecting a 1-dim ``_dummy_vec`` FLOAT_VECTOR
    field with a FLAT index that is never queried.

    Args:
        client: Async Milvus client.
        collection_name: Name of the collection to create.
        field_definitions: List of dicts with keys ``name`` and ``datatype``
            (``DataType`` value).  VARCHAR fields automatically receive
            ``max_length=varchar_max_length``.
        pk_field: Name of the primary-key field (must be present in
            ``field_definitions``).
        varchar_max_length: Default ``max_length`` for VARCHAR fields.
    """
    if await client.has_collection(collection_name):
        return

    schema = MilvusClient.create_schema(auto_id=False)

    # Milvus mandates at least one vector field; use a 2-dim dummy (dim must be >= 2).
    schema.add_field("_dummy_vec", DataType.FLOAT_VECTOR, dim=2)

    for fdef in field_definitions:
        fname = fdef["name"]
        ftype = fdef.get("datatype", DataType.VARCHAR)
        kwargs: dict[str, Any] = {
            "field_name": fname,
            "datatype": ftype,
            "description": fdef.get("description", ""),
        }
        if fname == pk_field:
            kwargs["is_primary"] = True
        if ftype == DataType.VARCHAR:
            kwargs["max_length"] = varchar_max_length
        schema.add_field(**kwargs)

    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(field_name="_dummy_vec", index_type="FLAT", metric_type="L2")

    await client.create_collection(
        collection_name=collection_name,
        schema=schema,
        index_params=index_params,
    )


async def select_all(
    client: AsyncMilvusClient,
    collection_name: str,
):
    """Select all entities from a collection."""
    if not await client.has_collection(collection_name):
        return []

    results = await client.query(
        collection_name=collection_name,
        filter="",
        output_fields=["*"],
        limit=16384,
        consistency_level=ConsistencyLevel.Strong,
    )
    return results
