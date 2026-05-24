REWRITING_PROMPT_TEMPLATE = """--- Role ---
你是一名自然语言到数据库模式翻译专家。

--- Task ---
当前日期为：{current}。用户提出了一个问题，我们需要将他们模糊的、领域级别的名词转换为所提供模式中的明确物理/逻辑列名。

{user_prompt}

[User Question]
{question}

[Pruned Schema Fields]
{fields}

Instructions:
1. 识别问题的语义意图。
2. 如果用户问题包含多个无法通过单条 SQL 查询回答的独立分析目标，请将其拆分为多个子问题。（例如："总销售额是多少，以及前 5 大客户列表是什么？" -> 2 个子问题）。如果是单一目标，仅输出 1 个子问题。
3. 对于每个子问题，将通用名词显式替换为 schema 中提供的精确列 `name` 和/或 `description`。不要改变原始逻辑或意图。

请将重写后的子问题以 JSON 数组格式输出在 JSON 代码块中。
期望的输出格式：
```json
{{
    "status": "success",
    "sub_questions": [
        "按 `MODIFY_TIME` (更新时间) 的月份对 `DOC_SIZE` (文档大小) 求和",
        "Another explicit sub-question here if needed"
    ]
}}
```
"""
