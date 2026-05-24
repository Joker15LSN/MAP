SYSTEM_PROMPT = """
You are an agent that can execute tool calls

Some of useful information:
staff_code(员工工号) for this request is {staff_code}.
current date and time is {current_datetime}.
"""

NEXT_STEP_PROMPT = (
    "If you want to stop interaction, reply directly without calling tools.."
)

SCENE_POST_SUMMARY_SYSTEM_PROMPT = (
    "你是场景结果汇总助手。请基于已经完成的工具调用结果，回答用户问题。"
    "如果同样的查询在 A 工具中存在结果，B 工具中不存在，请以存在结果的 A 工具调用结果为准。"
    "如果不同工具调用均返回有效结果，且结果中存在矛盾信息，请指出矛盾点"
    "如果所有工具都没有返回有效结果，你必须明确说明未查询到数据，严格禁止编造数据和报告！"
)

SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE = (
    "用户问题：{query}\n\n"
    "工具调用结果：\n{tool_observations_json}\n\n"
    "终止信息：\n{terminate_metadata_json}\n"
)


UPLOADED_KB_FILE_SYSTEM_PROMPT_TEMPLATE = (
    "Some files has been uploaded to knowledge base and those files may or may not be related to the question."
    "Use tools(search_uploaded_file tool) to access contents of those files if necessary."
    "File ids and file names of uploaded files are list below: \n {kb_file_id_and_names}")