"""
部门后端接口字段通用定义
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


# ===== 基础问答单元 =====
# 描述单条问句及其历史上下文，用于构建多轮会话；
# 下游 BPMRequest.history 引用 HistoryPair，HistoryPair 引用 QueryItem。
class QueryItem(BaseModel):
    context: str
    type: str


class HistoryPair(BaseModel):
    """一轮对话的问答集合，携带前后文。"""

    queryList: list[QueryItem]
    answerList: list[QueryItem]

    def first_query_context(self) -> str | None:
        """Return the context string of the first QueryItem in queryList.

        Returns None when the list is empty or the first item's context is missing.
        """
        if not getattr(self, "queryList", None):
            return None

        try:
            first = self.queryList[0]
        except Exception:
            return None

        ctx = getattr(first, "context", None)
        return ctx if isinstance(ctx, str) else (str(ctx) if ctx is not None else None)

    def first_answer_context(self) -> str | None:
        """Return the context string of the first QueryItem in answerList.

        Returns None when the list is empty or the first item's context is missing.
        Non-string contexts are converted to strings.
        """
        if not getattr(self, "answerList", None):
            return None

        try:
            first = self.answerList[0]
        except Exception:
            return None

        ctx = getattr(first, "context", None)
        return ctx if isinstance(ctx, str) else (str(ctx) if ctx is not None else None)


# ===== 算法配置 =====
# 控制召回、推理模式等策略开关；
# BPMRequest.algorithmSpecMap 直接持有该结构。
class AlgorithmSpecMap(BaseModel):
    mode: str | None = None
    # isKnowledge: bool | None = None
    # knowledges: Optional[Knowledges] = None
    tenantId: str | None = None


# ===== 用户侧上下文 =====
# 登录态、Cookie、租户等信息，供后台鉴权与个性化使用；
# BPMRequest.loginUser 引用 LoginUser，LoginUser.cookies 为 Cookie 列表。
class Cookie(BaseModel):
    comment: Any | None = None
    domain: Any | None = None
    httpOnly: bool | None = None
    maxAge: int | None = None
    name: str | None = None
    path: Any | None = None
    secure: bool | None = None
    value: str | None = None
    version: int | None = None


class LoginUser(BaseModel):
    authorization: str | None = None
    cookies: list[Cookie] | None = None
    language: str | None = None
    staffCode: str | None = None
    supToken: str | None = None
    tenantId: str | None = None
    userId: str | None = None
    userName: str | None = None


class ModelType(BaseModel):
    id: str | None = None
    value: str | None = None


# ===== 模型供应链 =====
# ModelType 定义模型类别；ChatSupplier 描述供应商；
# ChatModel 绑定供应商及密钥；ModelInfo 汇总最终模型配置；
# BPMRequest.model 引用 ModelInfo，用于选择调用的具体模型。
class ChatSupplier(BaseModel):
    algHandler: str | None = None
    code: str | None = None
    id: int | None = None
    name: str | None = None
    supplierType: ModelType | None = None
    url: str | None = None
    usageType: Any | None = None


class ChatModel(BaseModel):
    apiKey: Any | None = None
    busTag: str | None = None
    chatSupplier: ChatSupplier | None = None
    createTime: int | None = None
    id: int | None = None
    isAvailable: bool | None = None
    modelType: ModelType | None = None
    secretKey: Any | None = None
    usingFlag: Any | None = None


class ModelInfo(BaseModel):
    chatModel: ChatModel | None = None
    id: str | None = None
    max_tokens: str | None = None
    temperature: str | None = None
    top_p: str | None = None


# ===== BPM 业务交互载荷 =====
# BPMRequest 承载输入侧上下文与配置（依赖 QueryItem/HistoryPair、AlgorithmSpecMap、LoginUser、ModelInfo 等）；
# AnswerContext/BPMStreamPayload 用于流式响应片段（可携带算法配置补充）；
# BPMResponse 为最终聚合响应。
class BPMRequest(BaseModel):
    """BPM Request Model"""

    # compulsory fields
    queryList: list[QueryItem]
    history: list[HistoryPair] = []

    # optional fields
    algorithmSpecMap: AlgorithmSpecMap | None = None
    kbId: list[Any] | None = None
    loginUser: LoginUser | None = None
    model: ModelInfo | None = None
    questionId: int | None = None
    requestId: Any | None = None
    sessionId: int | None = None
    specMap: dict[str, Any] | None = None
    customizedSystemPrompt: str | None = None


class AnswerContext(BaseModel):
    context: str | dict
    type: Literal["text", "title", "file", "analysis"]


class BPMStreamPayload(BaseModel):
    """BPM 流式返回片段定义"""

    answerList: AnswerContext | None = None
    algorithmSpecMap: dict[str, Any] | None = None
    finishFlag: bool = False
    errCode: str | None = None  # 非空表示出错
    errMsg: str | None = None  # 非空表示出错


class BPMResponse(BaseModel):
    response: Any
