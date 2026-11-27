# ==========================================================
#  数据库模型
# ----------------------------------------------------------
#  设计要点（企业级和 Demo 的分水岭就在这里）：
#  1. 权限要能在 SQL 层表达 —— 所以用户带 dept_id，合同带 dept_id，
#     查询时直接 WHERE dept_id IN (...)，而不是查出来再在界面上藏。
#  2. 审计日志只增不改 —— 没有 update 接口，没有 delete 接口。
#  3. 合同按"条款块"存储 —— 因为财务只能看付款条款，
#     字段级权限必须落到块级别，不能靠 prompt 说"别提违约责任"。
#  4. prompt 版本化 —— 存数据库，带版本号，能一键回滚。
# ==========================================================
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """统一用 UTC 存库，展示时再转本地时区。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 组织与用户
class Department(Base):
    """部门 —— 权限隔离的第一层。"""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="department")


class User(Base):
    """
    用户。
    role 决定"能做什么"，dept_id 决定"能看到谁的东西"。
    两者是正交的 —— 法务审查员能看全部合同（跨部门），
    但业务提交人只能看自己部门的。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # submitter / reviewer / finance / admin
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="submitter")
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    department: Mapped["Department | None"] = relationship(back_populates="users")


# ---------------------------------------------------------------- 合同
class Contract(Base):
    """
    一份合同。
    status 状态机：uploaded → reviewing → reviewed → approved / rejected
    """

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")  # 去重用

    # 合同类型：采购 / 服务 / 租赁 / 保密 / 劳动 / 其他
    contract_type: Mapped[str] = mapped_column(String(32), default="其他")
    # 纯文本正文（解析后）
    raw_text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True)

    # 归属：提交人 + 部门。权限判断全靠这两个字段
    submitter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    submitter: Mapped["User"] = relationship(foreign_keys=[submitter_id])
    department: Mapped["Department | None"] = relationship(foreign_keys=[dept_id])
    clauses: Mapped[list["Clause"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class Clause(Base):
    """
    条款块 —— 企业级的关键设计。
    财务只能看 payment 类条款，所以权限必须能精确到"块"。
    visibility 是逗号分隔的角色列表，检索时先过滤再喂给模型。
    """

    __tablename__ = "clauses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)

    order_no: Mapped[int] = mapped_column(Integer, default=0)  # 第几条
    heading: Mapped[str] = mapped_column(String(255), default="")  # 条款标题，如"第六条 违约责任"
    content: Mapped[str] = mapped_column(Text, default="")

    # 条款分类：payment / liability / termination / confidentiality / dispute / other
    category: Mapped[str] = mapped_column(String(32), default="other", index=True)
    # 谁能看：all / submitter,reviewer / finance,reviewer / reviewer,admin ...
    visibility: Mapped[str] = mapped_column(String(128), default="all")

    contract: Mapped["Contract"] = relationship(back_populates="clauses")


# ---------------------------------------------------------------- 审查
class Review(Base):
    """
    一次审查 = 一次 LLM 调用。
    保留每次调用的完整上下文，因为你要能回答
    "这条意见是哪版 prompt、哪个模型、哪次检索出来的"。
    """

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)

    # 关联的 prompt 版本（可回滚的关键）
    prompt_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    model_name: Mapped[str] = mapped_column(String(64), default="")

    # 审查结果：JSON 数组，每项是一个风险点
    # [{clause_no, level, category, issue, suggestion, confidence}]
    findings: Mapped[list] = mapped_column(JSON, default=list)

    risk_level: Mapped[str] = mapped_column(String(16), default="none")  # none/low/medium/high
    summary: Mapped[str] = mapped_column(Text, default="")

    # 是否走了降级（模型挂了 → 规则引擎兜底）
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    degrade_reason: Mapped[str] = mapped_column(String(255), default="")

    # 成本
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, default=0.0)

    # 法务的反馈：这是数据飞轮的入口
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewer_action: Mapped[str] = mapped_column(String(32), default="")  # accepted/edited/rejected
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewer_edits: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    contract: Mapped["Contract"] = relationship(back_populates="reviews")


class PromptVersion(Base):
    """
    Prompt 版本化 —— 线上效果变差时，你要能在五分钟内回到昨天。
    每次修改 prompt 都新增一行，不覆盖旧行。
    """

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="contract_review")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    user_template: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)


# ---------------------------------------------------------------- 审计
class AuditLog(Base):
    """
    审计日志 —— append only。
    没有 update 方法，没有 delete 方法。合规审查就看这个。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- 评测
class EvalCase(Base):
    """评测集中的一条用例：一份带已知风险点的合成合同。"""

    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    contract_type: Mapped[str] = mapped_column(String(32), default="其他")
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")

    # 埋进去的风险点：[{clause_no, category, keyword, level}]
    planted_risks: Mapped[list] = mapped_column(JSON, default=list)
    # 正常条款数（用来算误报率的分母）
    normal_clause_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvalRun(Base):
    """一次评测跑分。每改一次 prompt / 换一次模型就跑一次，记录分数变化。"""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    model_name: Mapped[str] = mapped_column(String(64), default="")
    label: Mapped[str] = mapped_column(String(128), default="")

    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    planted_total: Mapped[int] = mapped_column(Integer, default=0)
    caught_total: Mapped[int] = mapped_column(Integer, default=0)
    false_positive_total: Mapped[int] = mapped_column(Integer, default=0)

    recall: Mapped[float] = mapped_column(Float, default=0.0)
    false_positive_rate: Mapped[float] = mapped_column(Float, default=0.0)
    f1: Mapped[float] = mapped_column(Float, default=0.0)
    avg_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
