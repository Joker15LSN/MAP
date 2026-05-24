from pydantic import BaseModel, Field, ValidationInfo, model_validator

from . import scene_registry

BigScene = str

SubScene = str

BIG_SCENE_TO_SUB_SCENES: dict[BigScene, list[SubScene]] = {
    big_scene: list(sub_scenes)
    for big_scene, sub_scenes in scene_registry.BIG_SCENE_TO_SUB_SCENES.items()
}


class SceneItem(BaseModel):
    reason: str = Field(description="简要中文解释，50 字以内，说明选择原因")
    big_scene: BigScene = Field(
        ...,
        description="命中的大场景",
        json_schema_extra={"enum": list(scene_registry.BIG_SCENES)},
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="你对分类的信心程度 (0-1)"
    )


class BigSceneClassificationResult(BaseModel):
    big_scenes: list[SceneItem] = Field(
        ..., min_length=1, max_length=5, description="按相关度排序的场景列表"
    )


class SceneClassificationResult(BaseModel):
    big_scenes: list[SceneItem] = Field(
        ..., min_length=1, max_length=5, description="按相关度排序的场景列表"
    )
    sub_scenes: list["SubSceneResult"] = Field(
        ..., description="按相关度排序的子场景列表"
    )


class SubSceneResult(BaseModel):
    big_scene: BigScene = Field(
        description="所属的大场景",
        json_schema_extra={"enum": list(scene_registry.BIG_SCENES)},
    )
    sub_scenes: list[SubScene] = Field(
        ...,
        min_length=1,
        description="选定的子场景列表，包含 1 个或多个最相关的子场景",
        json_schema_extra={"items": {"enum": list(scene_registry.SUB_SCENES)}},
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="你对分类的信心程度 (0-1)"
    )
    reason: str = Field(description="简要中文解释，50 字以内，说明选择原因")

    @model_validator(mode="after")
    def validate_sub_scene_under_big_scene(
        self, info: ValidationInfo
    ) -> "SubSceneResult":
        mapping = (
            info.context.get("big_scene_to_sub_scenes")
            if isinstance(info.context, dict)
            else None
        )
        target_mapping = mapping or BIG_SCENE_TO_SUB_SCENES
        allowed = set(target_mapping.get(self.big_scene, []))
        invalid = [
            sub_scene for sub_scene in self.sub_scenes if sub_scene not in allowed
        ]
        if invalid:
            invalid_text = ", ".join(invalid)
            raise ValueError(
                f"sub_scenes [{invalid_text}] do not belong to big_scene '{self.big_scene}'"
            )
        return self


# LLM 调用所需的 JSON Schema
SCENE_CLASSIFICATION_SCHEMA: dict[str, object] = (
    SceneClassificationResult.model_json_schema()
)
