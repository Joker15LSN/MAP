"""Shared business knowledge for LLM prompts."""

BUSINESS_KNOWLEDGE_CONTEXT = """
--- Knowledge ---
### 名词解释
**指标（Metrics）**：指标指的是用于衡量企业或组织特定方面表现的数据项。指标通常用于业务分析、绩效考核、财务监控等场景。每个指标都有明确的业务含义。
**维度（Dimensions）**：维度是用于对指标进行分类、切片和筛选的属性。例如："AR人数"这个指标可以按一级部门名称、职称级别或学历进行拆分。
**维度值（dimension_value）**：维度值是维度下的具体取值。例如："一级行业名称" 这个维度包含：油气、医药食品、化工等维度值。

### 维度与维度值信息：
1.铁三角岗位简称：AR（Account Responsible，客户责任人）、SR（Solution Responsible，解决方案责任人）、FR（Fulfillment Responsible，履行交付责任人）
2. 产品维度：
- 一级产品线：['第三方','杭州全世分析仪','其他','会员产品','SMI','PlantMart','POM','Industrial Robot','Industrial AI','Hobre','MAP','Automation']
注意：不要忽略了“其他“这个一级产品线


"""

_DRAFT = """
### 维度与维度值信息：
1.铁三角岗位简称：AR（Account Responsible，客户责任人）、SR（Solution Responsible，解决方案责任人）、FR（Fulfillment Responsible，履行交付责任人）
2.区域维度明细划分（一级部门名称）：
    国内区域：['中南大区', '华南大区', '西北大区', '华北大区', '东北大区', '西南大区', '华中大区', '华东大区', '蒙古特区（内蒙古）']
    海外区域: ['CA区域', 'EU区域', 'MEA区域', 'SEA区域', '蒙古特区（蒙古国）', 'JP&KR区域', 'AU区域', 'NA区域', 'LA区域', 'Hobre公司']
    每个区域下设细分的二级部门名称
3.产品维度明细划分：
    四大类: ['第三方', '仪器仪表', '工业软件', '控制系统']
    一级产品线：['西子','第三方','智慧园区','分析仪表','其他','全世科技公司','Smart Manufacturing','PlantMart','Industrial AI','Automation']
    二级产品线：['西子', '自动化仪表', '纯成套第三方', '现场仪表', '控制阀', '工业信息安全', '富阳公司', '分析仪表', '其他', '关键控制', '会员产品', '产品型第三方', 'MAP（Multi Agent Path）富阳公司', 'TPT', 'POM', 'Q-Lab', 'PRIDE', 'PLC', 'OMC', 'MAP', 'PlantMart']
    三级产品线: ['数据安全','销售支撑与保障','PRIDE','温度仪表','MAP','安全服务','supOS','SIS','紧凑型DCS','碳能优化','富阳公司','智慧实验室','安全优先','物位仪表','纯成套第三方','控制阀','振动监测与保护','关键控制','产品型第三方','其他','动设备管理','PLC','CCS','OMC','智慧园区','数字孪生','全设备管理','DAAS','分析仪表','供应链优化','中大型DCS','压力仪表','工业信息安全','装备PLC','通用PLC','在线分析仪','生产运营','其他新型仪表','UCS','Q-Lab','信号链','流程工业时序大模型','信息网安全','分析系统集成','会员产品','数据与资源','自主运行','现场仪表','流量仪表','西子','工控安全','Hobre','PlantMart','仪控设备管理','供应链管理']
4. 事业群(属于部门名称)：
    ['Industrial Solution 中心'，'Industrial AI 事业群'，'Automation 事业群'，'PlantMart 事业群'，'Multi-Industry 事业群'，'Smart Manufacturing 事业群']
"""
