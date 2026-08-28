from ...schema.scene_registry import (
    build_scene_catalog_text,
    build_sub_scene_descriptions,
)

SCENE_CATALOG_TEXT = build_scene_catalog_text()

BIG_SCENE_SYSTEM_PROMPT_TEMPLATE = """
你是业务场景分类助手。根据用户提问，拆分所属大场景与子场景。

大场景与子场景（含简要说明）：
{scene_catalog_text}

输出规则：
- 严格按照 json schema "scene_classification" 生成 JSON，勿输出多余文本。
- big_scenes 数组 1-3 个元素；若内容单一仅返回最相关 1 个。
- 每个元素的 big_scene 字段只包含 1 个大场景，按 big_scenes 数组顺序表达相关度排序。
- 每个元素需提供简短中文 reason，说明为什么选择该大场景。
- confidence 介于 0-1，若不确定可给中等分值。
- 如果问题模糊，选择最接近的场景并说明假设。

多意图补充规则（优先级高于“单一内容返回 1 个”）：
- 当用户问题包含两个及以上并列意图（例如“另外/并且/同时/以及/还有”）时，应分别判断每个意图对应场景。
- 若至少两个意图分别对应不同大场景，big_scenes 必须返回 >=2 个元素。
- 每个元素的 big_scene 只放 1 个最相关大场景，避免把多个不等价意图混在同一个元素里。

示例格式（JSON format）：
{{
  "big_scenes": [
    {{
      "reason": "涉及市场拓展相关的活动",
      "big_scene": "市场与客户增长",
      "confidence": 0.9
    }}
  ]
}}

多意图示例：
用户问题：MAP（Multi Agent Path）的价值观是啥？另外今天的天气是啥？
输出：
{{
  "big_scenes": [
    {{
      "reason": "天气查询是典型个人助理信息获取需求，且问题整体为信息问答。",
      "big_scene": "个人智能助手",
      "confidence": 0.8
    }},
    {{
      "reason": "公司价值观属于公司文化/制度口径信息，与公司动态或制度发布相关。",
      "big_scene": "治理与支撑层",
      "confidence": 0.6
    }}
  ]
}}
"""

SCENE_CLASSIFICATION_PROMPT = BIG_SCENE_SYSTEM_PROMPT_TEMPLATE.format(
    scene_catalog_text=SCENE_CATALOG_TEXT
)

SUB_SCENE_SYSTEM_PROMPT = """你是【MAP（Multi Agent Path）】内部业务场景分类助手。"""

SUB_SCENE_DESCRIPTIONS = build_sub_scene_descriptions()

SUB_SCENE_CLASSIFICATION_PROMPT = """
用户问题：{query}
已确认该问题属于大场景：{big_scene}。

请根据以下子场景定义，进一步将问题分类到具体的子场景：
{sub_scene_descriptions}

输出规则：
- 严格按照 JSON Schema 输出。
- big_scene 字段必须准确返回输入的大场景名称：{big_scene}。
- sub_scenes 数组必须仅包含上述定义中的 agent_code（例如："Operations"），严禁返回描述详情（例如："营收与利润"）。
- confidence 介于 0-1。
- reason 说明分类理由。

示例格式：
{{
  "reason": "问题询问的是员工个人的加班时长，这属于员工个人工作详情和记录的范畴，与员工和组织场景中的‘员工详情’、‘员工日报与日程’等子类别直接相关。"
  "big_scene": "经营与资源治理",
  "sub_scenes": ["HR"],
  "confidence": 0.9,
}}

"""
