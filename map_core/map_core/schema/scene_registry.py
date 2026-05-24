from __future__ import annotations

from typing import Any, Final, TypedDict

from pydantic import BaseModel, RootModel, field_validator, model_validator


class SceneConfig(TypedDict):
    description: str
    sub_scenes: dict[str, str]


class SceneConfigSchema(BaseModel):
    description: str
    sub_scenes: dict[str, str]

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("description cannot be empty")
        return normalized

    @field_validator("sub_scenes")
    @classmethod
    def validate_sub_scenes(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("sub_scenes cannot be empty")

        normalized: dict[str, str] = {}
        for sub_scene, description in value.items():
            normalized_sub_scene = sub_scene.strip()
            normalized_description = description.strip()

            if not normalized_sub_scene:
                raise ValueError("sub_scene name cannot be empty")
            if not normalized_description:
                raise ValueError(
                    f"sub_scene '{normalized_sub_scene}' description cannot be empty"
                )
            normalized[normalized_sub_scene] = normalized_description
        return normalized


class SceneRegistrySchema(RootModel[dict[str, SceneConfigSchema]]):
    @model_validator(mode="after")
    def validate_registry(self) -> "SceneRegistrySchema":
        if not self.root:
            raise ValueError("scene registry is empty")

        all_sub_scenes: list[str] = []
        for big_scene, config in self.root.items():
            normalized_big_scene = big_scene.strip()
            if not normalized_big_scene:
                raise ValueError("big scene name cannot be empty")

            all_sub_scenes.extend(config.sub_scenes.keys())

        if len(set(all_sub_scenes)) != len(all_sub_scenes):
            raise ValueError("duplicate sub scene names found across big scenes")

        return self


SCENE_REGISTRY: Final[dict[str, SceneConfig]] = {
    "class1": {
        "description": "MAP（Multi Agent Path）有限公司市场拓展、品牌与营销、客户需求挖掘。",
        "sub_scenes": {
            "Market_Assistant": "市场场景：市场调研、品牌宣传、活动投放、竞品与渠道分析。",
            "Customer_Assistant": "客户场景：客户画像与销售机会管理" \
                            "该场景可以查询丢标明细、竞争对手配置、客户基本信息、"\
                            "客户历史事故、客户装置、销售活动明细等数据表。",
            "Quality": "质量场景：质量指标与缺陷、审计与合规、持续改进、风险与问题闭环。" \
                "该场景可以查询LTC VOC客户满意度明细表、售后满意度调查表、VOC主表明细（客户抱怨/项目执行问题）" \
                "VOC表扬表明细、产品销售类型合同满意度调查表、开箱满意度调查表、用户培训满意度调查表",
            "Ecosystem_Partner": "生态合作场景：渠道伙伴、联营/加盟、联合营销、合作分成。" \
                                "该场景可以查询 生态地图 数据表。" ,
        },
    },
    "class2": {
        "description": "MAP（Multi Agent Path）有限公司产品规划、项目交付、供应与采购的运营管理。",
        "sub_scenes": {
            "IPD_RD": "IPD 场景场景：需求收集、产品规划、版本路线、研发迭代与发布。",
            "Engineering": "工程管理：项目计划/排期、进度里程碑、质量与风险管控、验收交付。"\
                    "该场景可以查询 四算完成率、经营目标达成率、工程项目运作指标表、" \
                    "100万以上每月核算情况、项目经营预警等数据表。",
            "Supply_Chain": "供应链场景：需求预测、库存/物流/仓储协同、供应商履约与成本控制。"\
                    "该场景可以查询 库龄监控模型表、呆滞品库存处理统计表、"\
                    "委托加工工单不良明细表、富阳园区厂房使用率、富阳园区租金收入明细、" \
                    "ITO战略库存消除、物料结存明细、物料出库汇总、货物运输明细、库存管理汇总等数据表。",

            "Procurement": "采购场景：采购寻源、比价招投标、合同与交付验收、供应商绩效。" \
                        "该场景可以查询 财务付款发票明细报表、库存成本报表、合同台账按月表、财务付款明细报表等数据表。",
        },
    },
    "class3": {
        "description": "MAP（Multi Agent Path）有限公司经营分析、人力与组织、质量与合规治理。",
        "sub_scenes": {
            "Operations": "经营分析场景：包括但不限于营业额、销售额、回款营收、利润、成本费用、预算与执行等等的数据。可以根据这些数据进行数据分析和帮助主管领导进行经营决策。",
            "HR": "员工和组织场景：处理人力资源有关问题及数据，包括但不限于：查询公司组织架构，周报日报，考勤出差，员工画像，员工日程，工作总结等。",
        },
    },
    "class4": {
        "description": "MAP（Multi Agent Path）有限公司公共事务、园区设施、数字化平台、流程制度建设。",
        "sub_scenes": {
            "Company_News": "公司动态新闻场景：公司新闻与内部动态和资料。包括但不限于公司发文，公司政策制度、规章流程文件，公司知识库，产品说明等等",
            "Park_Service": "公司园区场景：园区服务、安防后勤、资产设施、工位与访客管理。",
            "Digitalization": "数字化场景：数据平台、应用集成、低代码/自动化、主数据治理。",
            "Process_Assist": "流程体系场景：流程设计与优化、权限与内控、SOP/制度沉淀、审批配置。",
        },
    },
    "class5": {
        "description": "个人工作效率工具、文档问答、个人智能助理（支持文件读取和在临时沙箱执行bash命令）、闲聊、一切需要互联网检索才能得到的信息内容。",
        "sub_scenes": {
            "General_Assistant": "个人智能助手场景：个人工作效率工具、文档问答、个人智能助理（支持文件读取和在临时沙箱执行bash命令）、闲聊、一切需要互联网检索才能得到的信息内容。",
            "Industrial_Assistant": "工业亿问场景", # desc depends on params passed in.
            "Financial_Assistant": "财务助手场景", # desc depends on params passed in.
        },
    },
}


def normalize_scene_registry(
    registry: SceneRegistrySchema | dict[str, Any] | None = None,
) -> dict[str, SceneConfig]:
    target: SceneRegistrySchema | dict[str, Any] = registry or SCENE_REGISTRY
    validated = (
        target
        if isinstance(target, SceneRegistrySchema)
        else SceneRegistrySchema.model_validate(target)
    )
    return {
        big_scene: {
            "description": config.description,
            "sub_scenes": dict(config.sub_scenes),
        }
        for big_scene, config in validated.root.items()
    }


def validate_scene_registry(
    registry: SceneRegistrySchema | dict[str, Any] | None = None,
) -> None:
    normalize_scene_registry(registry)


def build_big_scene_to_sub_scenes(
    registry: SceneRegistrySchema | dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    target = normalize_scene_registry(registry)
    return {
        big_scene: list(payload["sub_scenes"].keys())
        for big_scene, payload in target.items()
    }


def build_sub_scene_descriptions(
    registry: SceneRegistrySchema | dict[str, Any] | None = None,
) -> dict[str, str]:
    target = normalize_scene_registry(registry)
    result: dict[str, str] = {}
    for big_scene, payload in target.items():
        lines = [
            f"- {sub_scene}：{sub_description}"
            for sub_scene, sub_description in payload["sub_scenes"].items()
        ]
        result[big_scene] = "\n" + "\n".join(lines) + "\n"
    return result


def build_scene_catalog_text(
    registry: SceneRegistrySchema | dict[str, Any] | None = None,
) -> str:
    target = normalize_scene_registry(registry)
    lines: list[str] = []
    for big_scene, payload in target.items():
        # lines.append(f"## big_scene {big_scene}：{payload['description']}")
        lines.append(f"- big_scene: {big_scene}：{payload['description']}")
        for sub_scene, sub_description in payload["sub_scenes"].items():
            lines.append(f"\t- sub_scene: {sub_scene}：{sub_description}")
            # lines.append(f"\t ### sub_scene {sub_scene}：{sub_description}")
    return "\n".join(lines)


# Validate the registry at module load time to catch errors early
validate_scene_registry()

BIG_SCENES: Final[tuple[str, ...]] = tuple(SCENE_REGISTRY.keys())
SUB_SCENES: Final[tuple[str, ...]] = tuple(
    sub_scene
    for payload in SCENE_REGISTRY.values()
    for sub_scene in payload["sub_scenes"].keys()
)

BIG_SCENE_TO_SUB_SCENES: Final[dict[str, list[str]]] = build_big_scene_to_sub_scenes()
