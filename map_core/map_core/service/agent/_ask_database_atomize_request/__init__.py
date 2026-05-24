"""
问表拆解模块

因为有行列权限，没有办法使用单表进行查询（关联的表选择的可访问的行，自动筛选了连接的表能访问的行）

so 确定一个宽表模型就是一张表

包括：
- 宽表模型选取
- 问题拆解 + 改写
"""

from ._schema import (
    AtomizeContext,
    DecomposedTask,
    FilteredDataModel,
    PrunedSchema,
)
from .atomize_request import atomize_question, process_atomize_pipeline
from .process_schema import process_schema
from .select_data_models import select_data_models

__all__ = [
    "AtomizeContext",
    "FilteredDataModel",
    "PrunedSchema",
    "DecomposedTask",
    "select_data_models",
    "process_schema",
    "atomize_question",
    "process_atomize_pipeline",
]
