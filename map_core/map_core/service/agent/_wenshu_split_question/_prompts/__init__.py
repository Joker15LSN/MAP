"""
问数拆解提示词
开放出来的提示词

系统提示词：包括公司业务逻辑、指标和维度的定义、拆解 instructions

用户提示词：用户自定义的拆解模版

*总结提示词：用户自定义的总结模版 - 在问数子 agent 中使用*

IDENTIFY_METRICS_SYSTEM_PROMPT 被注入：系统提示词，用户提示词（提示模型参考拆解模版里的指标信息可能是用户关注的指标）

GENERATE_SUB_QUESTIONS_SYSTEM_PROMPT 被注入：系统提示词（主要提供拆解 instructions），用户提示词（提示模型参考拆解模版来拆解问题）

"""

################## 用户可配置 ##################
from ._split_question_examples import SPLIT_QUESTION_EXAMPLES
from ._split_question_instructions import SPLIT_QUESTION_INSTRUCTIONS

################## 系统配置 ##################
from .business_knowledge import BUSINESS_KNOWLEDGE_CONTEXT
from .system_prompt import (
    GENERATE_SUB_QUESTIONS_SYSTEM_PROMPT,
    IDENTIFY_METRICS_SYSTEM_PROMPT,
)
from .user_prompt import (
    GENERATE_SUB_QUESTIONS_USER_PROMPT,
    IDENTIFY_METRICS_USER_PROMPT,
)

__all__ = [
    "SPLIT_QUESTION_EXAMPLES",
    "SPLIT_QUESTION_INSTRUCTIONS",
    "BUSINESS_KNOWLEDGE_CONTEXT",
    "GENERATE_SUB_QUESTIONS_SYSTEM_PROMPT",
    "IDENTIFY_METRICS_SYSTEM_PROMPT",
    "GENERATE_SUB_QUESTIONS_USER_PROMPT",
    "IDENTIFY_METRICS_USER_PROMPT",
]
