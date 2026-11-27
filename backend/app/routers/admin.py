# ==========================================================
#  审计 / 看板 / Prompt 版本 路由
# ----------------------------------------------------------
#  审计日志：只有管理员能看，且只能读，没有写改删的接口
#  看板：管理员的成本与质量视图
#  Prompt 版本：管理员可切换，一键回滚
# ==========================================================
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AuditLog, Contract, PromptVersion, Review
from ..schemas import (
    AuditOut,
    DashboardStats,
    PromptVersionDetail,
    PromptVersionOut,
)
from ..security import CurrentUser, require_roles, write_audit

router = APIRouter(prefix="/api", tags=["管理与审计"])


from fastapi import Query

# ---------------------------------------------------------------- 审计日志
@router.get("/audit", response_model=dict)
def list_audit(
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
    action: str | None = None,
    username: str | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
):
    """
    审计日志查询（分页）。只有管理员可见。

    注意：这里没有 POST/PUT/DELETE —— 审计日志只能追加。
    任何"能改审计日志"的设计，在合规审查里都是直接不合格。
    """
    from sqlalchemy import func

    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username == username)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    stmt = stmt.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size)
    items = db.scalars(stmt).all()
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [AuditOut.model_validate(a) for a in items],
    }


@router.get("/audit/actions")
def list_audit_actions(
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
):
    """审计动作种类清单，给前端做筛选下拉。"""
    rows = db.execute(
        select(AuditLog.action, func.count(AuditLog.id)).group_by(AuditLog.action)
    ).all()
    return [{"action": a, "count": n} for a, n in rows]


@router.get("/audit/export")
def export_audit(
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
    action: str | None = None,
    username: str | None = None,
):
    """导出审计日志 CSV。"""
    from fastapi.responses import StreamingResponse
    import csv
    import io

    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username == username)
    stmt = stmt.order_by(AuditLog.id.desc())

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        # Header
        writer.writerow(["时间", "操作人", "操作", "动作码", "对象", "结果", "IP"])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for a in db.scalars(stmt).all():
            writer.writerow([
                a.created_at.isoformat() if a.created_at else "",
                a.username or "（未登录）",
                a.action,
                a.action,
                f"{a.target_type}#{a.target_id}" if a.target_id else a.target_type,
                "成功" if a.success else "失败",
                a.ip_address or "",
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit-log-{datetime.now(timezone.utc).date().isoformat()}.csv"'},
    )


# ---------------------------------------------------------------- 看板
@router.get("/dashboard", response_model=DashboardStats)
def dashboard(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """
    数据看板。

    注意权限：管理员看全局；其他人只看自己可见范围内的数据。
    """
    base_cond = []
    if user.role not in ("reviewer", "admin", "finance"):
        if user.dept_id is not None:
            base_cond.append(Contract.dept_id == user.dept_id)
        else:
            base_cond.append(Contract.submitter_id == user.id)

    def count_contracts(*extra):
        stmt = select(func.count(Contract.id))
        for c in base_cond + list(extra):
            stmt = stmt.where(c)
        return db.scalar(stmt) or 0

    total = count_contracts()
    reviewed = count_contracts(Contract.status == "reviewed")
    pending = total - reviewed

    # 审查统计
    rev_stmt = select(Review).join(Contract, Review.contract_id == Contract.id)
    for c in base_cond:
        rev_stmt = rev_stmt.where(c)
    reviews = db.scalars(rev_stmt).all()

    # 同一份合同可能被重复审查：风险等级与意见数取最新一次，
    # 否则会出现「高风险合同 8 份」但合同总数只有 7 份的矛盾。
    latest_by_contract: dict[int, Review] = {}
    for r in reviews:
        current = latest_by_contract.get(r.contract_id)
        if current is None or r.id > current.id:
            latest_by_contract[r.contract_id] = r
    latest_reviews = list(latest_by_contract.values())

    total_findings = sum(len(r.findings or []) for r in latest_reviews)
    high_risk = sum(1 for r in latest_reviews if r.risk_level == "high")
    degraded = sum(1 for r in reviews if r.degraded)
    total_tokens = sum(r.total_tokens for r in reviews)
    total_cost = round(sum(r.cost_cny for r in reviews), 4)
    # 降级审查走规则引擎、耗时为 0ms；混进平均值会把真实模型耗时拉低，
    # 这里只统计真正调了模型的审查。
    llm_reviews = [r for r in reviews if not r.degraded]
    avg_latency = (
        int(sum(r.latency_ms for r in llm_reviews) / len(llm_reviews)) if llm_reviews else 0
    )

    # 采纳率
    judged = [r for r in reviews if r.reviewer_action]
    accept_rate = (
        round(sum(1 for r in judged if r.reviewer_action in ("accepted", "edited")) / len(judged), 3)
        if judged
        else 0.0
    )

    # 风险分布（每份合同只算最新一次审查）
    by_risk: dict[str, int] = {}
    for r in latest_reviews:
        by_risk[r.risk_level] = by_risk.get(r.risk_level, 0) + 1

    # 问题类别分布（与「风险意见总数」口径一致）
    by_category: dict[str, int] = {}
    for r in latest_reviews:
        for f in r.findings or []:
            cat = f.get("category", "other")
            by_category[cat] = by_category.get(cat, 0) + 1

    # 最近审查
    recent = []
    for r in sorted(reviews, key=lambda x: x.id, reverse=True)[:10]:
        contract = db.get(Contract, r.contract_id)
        # 补上 UTC 标记，否则前端会把 UTC 当本地时间显示
        created = (
            r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        )
        recent.append(
            {
                "contract_id": r.contract_id,
                "title": contract.title if contract else "",
                "risk_level": r.risk_level,
                "findings": len(r.findings or []),
                "degraded": r.degraded,
                "latency_ms": r.latency_ms,
                "created_at": created.isoformat(),
            }
        )

    return DashboardStats(
        total_contracts=total,
        reviewed_contracts=reviewed,
        pending_contracts=pending,
        total_findings=total_findings,
        high_risk_contracts=high_risk,
        degraded_reviews=degraded,
        total_tokens=total_tokens,
        total_cost_cny=total_cost,
        avg_latency_ms=avg_latency,
        review_accept_rate=accept_rate,
        by_risk=by_risk,
        by_category=by_category,
        recent=recent,
    )


# ---------------------------------------------------------------- Prompt 版本
@router.get("/prompts", response_model=list[PromptVersionOut])
def list_prompts(
    user: Annotated[CurrentUser, Depends(require_roles("admin", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
):
    pvs = db.scalars(
        select(PromptVersion).order_by(PromptVersion.version.desc())
    ).all()
    return [PromptVersionOut.model_validate(p) for p in pvs]


@router.get("/prompts/{prompt_id}", response_model=PromptVersionDetail)
def get_prompt(
    prompt_id: int,
    user: Annotated[CurrentUser, Depends(require_roles("admin", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
):
    pv = db.get(PromptVersion, prompt_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="prompt 版本不存在")
    return PromptVersionDetail.model_validate(pv)


@router.post("/prompts/{prompt_id}/activate")
def activate_prompt(
    prompt_id: int,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
):
    """
    切换生效的 prompt 版本 —— 这就是"一键回滚"。

    线上效果变差时，管理员点一下就能回到上一版，不用改代码、不用重启。
    """
    target = db.get(PromptVersion, prompt_id)
    if target is None:
        raise HTTPException(status_code=404, detail="prompt 版本不存在")

    previous = db.scalars(
        select(PromptVersion).where(PromptVersion.is_active.is_(True))
    ).all()
    prev_info = [{"id": p.id, "version": p.version} for p in previous]

    for p in previous:
        p.is_active = False
    target.is_active = True
    db.commit()

    write_audit(
        db, user=user, action="prompt_activate", target_type="prompt",
        target_id=target.id,
        detail={"new_version": target.version, "previous": prev_info},
        request=request,
    )
    return {
        "ok": True,
        "active_version": target.version,
        "previous": prev_info,
        "message": f"已切换到 v{target.version}，立即对后续审查生效",
    }
