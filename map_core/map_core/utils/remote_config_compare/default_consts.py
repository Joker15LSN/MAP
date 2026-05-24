SCENE_CODE_2_NAME = {
    "Procurement": "采购助手",
    "Company_News": "公司动态助手",
    "MASTER": "master助手",
    "Park_Service": "园区服务助手",
    "General_Assistant": "通用助手",
    "HR": "HR 助手",
    "IPD_RD": "IPD 研发助手",
    "Supply_Chain": "供应链助手",
    "Customer_Assistant": "客户管理助手",
    "Engineering": "工程管理助手",
    "Market_Assistant": "市场分析助手",
    "Quality": "质量管理助手",
    "Operations": "经营分析助手",
    "Ecosystem_Partner": "生态合作助手",
    "Industrial_Assistant": "工业亿问助手",
    "Financial_Assistant": "财务分析助手",
}

# DEFAULT_SUB_SCENE_USER_PROMPT_TEMPLATE = \
# "MAP（Multi Agent Path）项目/产品背景信息: 一级产品线: ['第三方', '杭州全世分析仪', '其他', '会员产品', 'SMI', 'PlantMart', 'POM', 'Industrial Robot', 'Industrial AI', 'Hobre', 'MAP', 'Automation']\n二级产品线: ['设备健康', '西子', '自主运行', '纯成套第三方', '现场仪表', '物流机器人', '模拟优化', '智汇元', '控制阀', '工业信息安全', '巡检机器人', '宁波全世', '在线分析仪', '分析系统集成', '关键控制', '产品型第三方', 'supOS', 'other', 'UCS', 'PLC', 'PCBA', 'ManuCloud', 'MOMCore', 'Hobre分析仪', 'MAP-UBD', 'MAP-Agent']\n三级产品线: ['通用控制系统', '通用PLC', '装备PLC', '纯成套第三方', '紧凑型DCS', '数据安全', '振动监测与保护', '工控安全', '安全服务', '安全SmartSite', '安全SES', '信息网安全', '供应链（储运）', '产品型第三方', '中大型DCS', 'SIS', 'SAAS灵动', 'OS+实时数据库', 'MES(MES-B、MES-P)', 'CCS']\n\n用户问题：{query}\n已确认该问题属于大场景：{big_scene}。\n可以参考的上一轮对话（仅当你认为话题有关时）：{history_context}。\n\n请根据以下子场景定义，进一步将问题分类到具体的子场景：\n{sub_scene_descriptions}\n\n输出规则：\n- 严格按照 JSON Schema 输出。\n- big_scene 字段必须准确返回输入的大场景名称：{big_scene}。\n- sub_scenes 数组必须仅包含上述定义中的子场景名称（例如：\"经营场景\"），严禁返回描述详情（例如：\"营收与利润\"）。\n- confidence 介于 0-1。\n- reason 说明分类理由。\n- 对于宽泛提问（例如用语包含‘怎么样’，‘如何’等），必须分发到多个场景。\n- 当问题符合多个场景描述，你应该选择多个符合的场景。"
# (
#     "MAP（Multi Agent Path）项目/产品背景信息: 一级产品线: ['第三方', '杭州全世分析仪', '其他', '会员产品', 'SMI', 'PlantMart', 'POM', 'Industrial Robot', 'Industrial AI', 'Hobre', 'MAP', 'Automation']\n"
#     "二级产品线: ['设备健康', '西子', '自主运行', '纯成套第三方', '现场仪表', '物流机器人', '模拟优化', '智汇元', '控制阀', '工业信息安全', '巡检机器人', '宁波全世', '在线分析仪', '分析系统集成', '关键控制', '产品型第三方', 'supOS', 'other', 'UCS', 'PLC', 'PCBA', 'ManuCloud', 'MOMCore', 'Hobre分析仪', 'MAP-UBD', 'MAP-Agent']\n"
#     "三级产品线: ['通用控制系统', '通用PLC', '装备PLC', '纯成套第三方', '紧凑型DCS', '数据安全', '振动监测与保护', '工控安全', '安全服务', '安全SmartSite', '安全SES', '信息网安全', '供应链（储运）', '产品型第三方', '中大型DCS', 'SIS', 'SAAS灵动', 'OS+实时数据库', 'MES(MES-B、MES-P)', 'CCS']\n\n"
#     "用户问题：{query}\n"
#     "已确认该问题属于大场景：{big_scene}。\n"
#     "可以参考的上一轮对话（仅当你认为话题有关时）：{history_context}。\n"
#     "请根据以下子场景定义，进一步将问题分类到具体的子场景：\n"
#     "{sub_scene_descriptions}\n\n"
#     "输出规则：\n"
#     "- 严格按照 JSON Schema 输出。\n"
#     "- big_scene 字段必须准确返回输入的大场景名称：{big_scene}。\n"
#     "- sub_scenes 数组必须仅包含上述定义中的子场景名称（例如：\"经营场景\"），严禁返回描述详情（例如：\"营收与利润\"）。\n"
#     "- confidence 介于 0-1。\n"
#     "- reason 说明分类理由。\n"
#     "- 对于宽泛提问（例如用语包含'怎么样'，'如何'等），必须分发到多个场景。\n"
#     "- 当问题符合多个场景描述，你应该选择多个符合的场景。"
# )
