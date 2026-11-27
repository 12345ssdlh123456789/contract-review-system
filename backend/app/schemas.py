# ==========================================================
#  Pydantic 数据模型（请求/响应的契约）
# ----------------------------------------------------------
#  为什么要有这一层：它同时干三件事
#  1. 请求校验（字段缺失/类型错自动 422，不用自己写 if）
#  2. 响应裁剪（password_hash 这种字段永远不会漏出去）
#  3. 自动生成 API 文档
# ==========================================================
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def iso_utc(value: datetime) -> str:
    """
    数据库里存的是 UTC 的 naive datetime，直接序列化会丢掉时区信息，
    浏览器会把 05:26Z 当成 05:26+08:00 显示 —— 所有时间都差 8 小时。
    所以出口统一补上 Z（UTC）。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 所有对外暴露的时间字段都用它，前端 new Date() 才能解析成正确时刻
UtcDatetime = Annotated[datetime, PlainSerializer(iso_utc, return_type=str)]


# ---------------------------------------------------------------- 认证
class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str
    role_name: str = ""
    dept_id: int | None = None
    dept_name: str = ""
    is_active: bool = True
    last_login_at: UtcDatetime | None = None


# ---------------------------------------------------------------- 分页通用
class PageParams(BaseModel):
    page: int = 1
    size: int = 20

    class Config:
        extra = "allow"


class PageResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list


# ---------------------------------------------------------------- 合同
class ClauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: int
    heading: str
    content: str
    category: str
    visibility: str


class ContractOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    original_filename: str
    file_size: int
    contract_type: str
    char_count: int
    status: str
    submitter_id: int
    dept_id: int | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    submitter_name: str = ""
    dept_name: str = ""
    risk_level: str = "none"
    finding_count: int = 0


class ContractListResponse(PageResponse):
    items: list[ContractOut] = []


class ContractDetail(ContractOut):
    clauses: list[ClauseOut] = []
    # 条款总数（含当前用户无权查看的）——
    # 前端靠它提示「有 N 条因权限不可见」，否则用户会以为 AI 漏审了
    clause_total: int = 0
    latest_review: "ReviewOut | None" = None
    # 创建接口专用：内容与已有合同重复时返回原合同，并置为 True
    duplicate: bool = False


class ContractCreateText(BaseModel):
    """直接粘贴文本创建合同（演示/测试用，不走文件上传）。"""
    title: str = Field(min_length=1, max_length=255)
    contract_type: str = "其他"
    content: str = Field(min_length=1)


# ---------------------------------------------------------------- 审查
class Finding(BaseModel):
    clause_no: int
    level: str
    category: str
    issue: str
    suggestion: str = ""
    confidence: float = 0.7
    source: str = "llm"


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    contract_id: int
    prompt_version_id: int | None = None
    model_name: str
    findings: list[Finding] = []
    risk_level: str
    summary: str
    degraded: bool
    degrade_reason: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    cost_cny: float
    reviewer_id: int | None = None
    reviewer_action: str = ""
    reviewer_note: str = ""
    # 条款级反馈（clause_feedbacks）也要回给前端，否则刷新后看不到哪条已经处理过
    reviewer_edits: dict | None = None
    created_at: UtcDatetime
    reviewed_at: UtcDatetime | None = None


class ReviewFeedback(BaseModel):
    """法务对 AI 意见的反馈 —— 数据飞轮的入口。"""
    action: str = Field(pattern="^(accepted|edited|rejected)$")
    note: str = ""
    edits: dict | None = None


# ---------------------------------------------------------------- 审计
class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    action: str
    target_type: str
    target_id: int | None = None
    detail: dict | None = None
    ip_address: str
    success: bool
    created_at: UtcDatetime


class AuditListResponse(PageResponse):
    items: list[AuditOut] = []


class AuditExportRequest(BaseModel):
    action: str | None = None
    username: str | None = None


# ---------------------------------------------------------------- Prompt 版本
class PromptVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: int
    note: str
    is_active: bool
    created_at: UtcDatetime


class PromptVersionDetail(PromptVersionOut):
    system_prompt: str
    user_template: str


# ---------------------------------------------------------------- 看板
class DashboardStats(BaseModel):
    total_contracts: int
    reviewed_contracts: int
    pending_contracts: int
    total_findings: int
    high_risk_contracts: int
    degraded_reviews: int
    total_tokens: int
    total_cost_cny: float
    avg_latency_ms: int
    review_accept_rate: float
    by_risk: dict[str, int] = {}
    by_category: dict[str, int] = {}
    recent: list[dict] = []


# ---------------------------------------------------------------- 评测
class EvalCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: str
    contract_type: str
    title: str
    planted_count: int = 0
    normal_clause_count: int = 0


class EvalRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prompt_version_id: int | None = None
    model_name: str
    label: str
    total_cases: int
    planted_total: int
    caught_total: int
    false_positive_total: int
    recall: float
    false_positive_rate: float
    f1: float
    avg_latency_ms: int
    total_tokens: int
    detail: dict | None = None
    created_at: UtcDatetime


class EvalRunListResponse(PageResponse):
    items: list[EvalRunOut] = []


class EvalExportRequest(BaseModel):
    pass  # 无参数，导出全部


# ---------------------------------------------------------------- 解析器验证（前端实时验证用）
class ParserValidateRequest(BaseModel):
    content: str


class ParsedClauseInfo(BaseModel):
    order_no: int
    heading: str
    content: str
    category: str
    visibility: str


class ParserValidateResponse(BaseModel):
    ok: bool
    clauses: list[ParsedClauseInfo] = []
    quality: dict = {}
    error: str = ""
