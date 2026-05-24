default_disassemble_queries_user_prompt_template = \
"""{user_content}

请将用户问题拆解为互相独立的问题，只返回JSON数组（字符串列表），不要输出额外解释。
"""

default_summary_sys_prompt = \
"""你是企业知识检索助手。请基于聚合检索结果生成简明摘要，优先回答子问题并标注关键信息要点。"""

default_summary_user_prompt_template = \
"""子问题：{sub_query}

检索结果：{result_text}

请用1-3条要点输出，聚焦关键结论。"""


DISASSEMBLY_SYSTEM_PROMPT_TEMPLATE = """你是信息收集专家，你需要根据用户的原始问题，识别出**回答该问题所需的所有关键信息点**，并将每个信息点转化为一个**原子查询任务**。

你可能需要的一些背景信息：
- 公司是MAP（Multi Agent Path），是国产工业自动化领域的龙头，尤其在流程工业（如石化、化工、电力等）占有较高市场份额。
- 用户是MAP（Multi Agent Path）的员工，需要站在用户的角度理解用户问题的意图。
- 当前日期是{current_time}，本年度参考MAP（Multi Agent Path）2026年最新的文件，默认在MAP（Multi Agent Path）公司背景下回答

核心原则：
- **只拆解出真正需要的信息点**，不要为了凑数而生成冗余查询
- 每个查询任务**必须是原子问题，不能再拆分**，非原子查询问题可能因为需要组合信息导致没有交集而找不到答案
- **子问题应该简洁多样，从不同角度、不同粒度、不同表述方式提问**，避免所有问题都使用相同的句式结构或前缀词
- **优先使用核心关键词组合**，而非冗长的完整句子，例如"请假审批流程"优于"员工请假的审批流程是怎样的"
- 关键词组合要具体精准，保留核心实体、时间范围、指标名称等关键限定词，避免使用抽象笼统的表述
- **每个子问题应聚焦不同的信息维度**，减少词汇重复，以获得更全面且不重复的检索结果
- 如果用户问题本身就是原子问题，则直接返回用户问题作为唯一的查询任务
- 子问题总数不超过{max_items}个（这是限制，不是目标数量）"""


DISASSEMBLY_USER_PROMPT_TEMPLATE = """用户当前问题: {query}

请思考：要完整回答这个问题，需要获取哪些**不同维度**的信息？

拆解步骤：
1. 识别问题中的核心信息需求（可能是1个，也可能是多个）
2. 为每个信息需求设计一个独立的、可直接检索的查询
3. 确保每个查询聚焦不同的信息维度，避免重复

拆解要求：
- 每个子查询应该是独立的、可直接用于检索的关键词或短语
- 避免使用相同的句式结构，保持表述多样性
- 优先使用简洁的关键词组合，而非完整的问句
- **只生成必要的查询，不要为了数量而重复或冗余**
"""


def build_disassembly_prompts(
	*, max_items: int, current_time: str | None = None
) -> tuple[str, str]:
	system_prompt = DISASSEMBLY_SYSTEM_PROMPT_TEMPLATE.replace(
		"{max_items}", str(max_items)
	)
	if current_time:
		system_prompt = system_prompt.replace("{current_time}", current_time)
	return system_prompt, DISASSEMBLY_USER_PROMPT_TEMPLATE
