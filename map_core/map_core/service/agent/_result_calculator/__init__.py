r"""
结果计算模块

计算：

- 求和：sum(value)
- 同比：Year-over-Year, YoY
    - 年同比：Year-over-Year Growth, YoY
$$ \text{YoY Growth (\%)} = \left( \frac{\text{Current Year Value} - \text{Previous Year Value}}{\text{Previous Year Value}} \right) \times 100 $$
    - 月同比：Year-over-Year Monthly Growth, YoY Monthly
$$ \text{YoY Monthly Growth (\%)} = \left( \frac{\text{Current Month Value} - \text{Same Month Previous Year Value}}{\text{Same Month Previous Year Value}} \right) \times 100 $$
    - 季同比：Year-on-Year Quarterly Growth, YoY Quarterly
$$ \text{YoY Quarterly Growth (\%)} = \left( \frac{\text{Current Quarter Value} - \text{Same Quarter Previous Year Value}}{\text{Same Quarter Previous Year Value}} \right) \times 100 $$
    - 日同比：Year-on-Year Daily Growth, YoY Daily
$$ \text{YoY Daily Growth (\%)} = \left( \frac{\text{Current Day Value} - \text{Same Day Previous Year Value}}{\text{Same Day Previous Year Value}} \right) \times 100 $$
- 环比：Month-over-Month, MoM
    - 月环比：Month-over-Month Growth, MoM
$$ \text{MoM Growth (\%)} = \left( \frac{\text{Current Month Value} - \text{Previous Month Value}}{\text{Previous Month Value}} \right) \times 100 $$
    - 季环比：Quarter-over-Quarter Growth, QoQ
$$ \text{QoQ Growth (\%)} = \left( \frac{\text{Current Quarter Value} - \text{Previous Quarter Value}}{\text{Previous Quarter Value}} \right) \times 100 $$
    - 日环比：Day-over-Day Growth, DoD
$$ \text{DoD Growth (\%)} = \left( \frac{\text{Current Day Value} - \text{Previous Day Value}}{\text{Previous Day Value}} \right) \times 100 $$
- 百分比：value / total
$$ \text{Percentage (\%)} = \left( \frac{\text{Value}}{\text{Total}} \right) \times 100 $$


计算工具
根据问题、原始数据，选择以上计算方法，将数据进行处理并计算

原始数据：
list[dict[str, Any]]

- time: str 原始数据中的时间字段，format: YYYY, YYYY-MM, YYYY-MM-DD
- value: int | float 指标值

e.g. 计算2026年1月至2026年3月的合同额的月环比

原始数据：
[
    {"time": "2026-01", "value": 100},
    {"time": "2026-02", "value": 200},
    {"time": "2026-03", "value": 300},
]

可算 MoM of 2026-02 and 2026-03

MoM Growth of 2026-02: (200-100)/100 = 1
MoM Growth of 2026-03: (300-200)/200 = 0.5



TODO
设计计算工具，输入为：

- 问题：str
- 原始数据：list[dict[str, Any]]
    - time: str
    - value: int | float

输出为：
{
    "metric": str,
    "calculation_type": str,
    "results": dict[str, Any],  # key 所有可算的统计结果，value对应的计算结果
}

NOTE:

- 一个问题只包含一种 calculation_type，如果问题有多种计算需求，refuse
- 对于 calculation_type，需要判断原始数据是否包含需要的时间

"""
