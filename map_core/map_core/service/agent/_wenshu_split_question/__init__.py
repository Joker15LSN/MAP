"""
Wenshu split question module (问数问题拆解模块).

本模块的主要目标是将用户复杂的自然语言问题拆解为多个针对独立指标的子问题，以便后续分别进行指标数据的查询。

## 核心工作流 (Workflow)

整个子问题拆解的过程分为以下关键步骤：
1. **并行获取基础维度与指标信息**：通过连接 Milvus 数据库（`metric` 与 `dimension` 集合），完整取出所有的指标定义（指标名称、代码、含义、绑定的维度等）以及基础的维度取值集合。
2. **文本向量化与维度值匹配**：将用户的自然语言提问做 Embedding 向量化。针对每一个可能相关的维度，向对应的 `dimension_{dimension_code}` 集合进行混合向量检索（Hybrid Search），通过相似度及模糊匹配打分，最终留下确实出现在用户原提问中的准确维度值。
3. **LLM 识别相关指标**：把用户提问、前置匹配好的维度值信息和全部可用指标传入 LLM 模型中。由大模型基于内置的“销售/经营/人力域业务知识”进行分析，从全局的所有指标中挑选出对回答用户问题最有帮助的指标列表。
4. **LLM 生成子问题 (并行)**：针对每 1 个被挑选出的指标，我们并行地调用 LLM 生成专门针对该指标的一至多条子问题。提示词中会包含严格的改写逻辑要求。
5. **组合结果**：将每个指标与其生成的子问题组合成结构化的数据返回，供外层的 `WenshuAgent` 等调用。

### 流程图 (Flowchart)

```mermaid
graph TD
    A([用户自然语言提问]) --> B[获取所有可用指标 (Milvus: metric)]
    A --> C[获取可用维度及部分枚举值 (Milvus: dimension)]

    A --> D(User Query Embedding 向量化)
    D --> E[语义搜索+模糊匹配获取确切提及的维度值]
    C --> E

    B --> F((LLM 指标召回与筛选))
    E --> F

    F --> |输出选定的 M 个指标| G{{并行生成子问题}}

    G --> |对 指标 1 | H1[LLM改写: 提取必要时间+维度生成问题]
    G --> |对 指标 2 | H2[LLM改写: 提取必要时间+维度生成问题]
    G --> |对 指标 M | HM[LLM改写: 提取必要时间+维度生成问题]

    H1 --> I([聚合返回：含不同指标的多个子问题])
    H2 --> I
    HM --> I
```
"""

from ._schema import (
    MILVUS_DB_NAME_PREFIX,
)
from .split_question import split_question

__all__ = [
    "MILVUS_DB_NAME_PREFIX",
    "split_question",
]
