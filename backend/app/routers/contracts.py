# ==========================================================
#  合同路由：上传 / 列表 / 详情 / 审查 / 反馈
# ----------------------------------------------------------
#  权限设计在这里体现得最完整：
#   - 列表：SQL 层过滤，越权的行根本不会查出来
#   - 详情：条款级过滤，财务看不到 liability 类条款
#   - 审查：只把"可见条款"喂给模型
#   - 反馈：只有法务/管理员能写
# ==========================================================
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import config
from ..database import get_db
from ..models import AuditLog, Clause, Contract, Department, Review, User
from ..parser import content_hash, parse_contract
from ..review_service import review_contract, save_review
from ..schemas import (
    ClauseOut,
    ContractCreateText,
    ContractDetail,
    ContractOut,
    ReviewFeedback,
    ReviewOut,
)
from ..security import (
    CurrentUser,
    clause_visible,
    contract_visibility_filter,
    require_roles,
    write_audit,
)

router = APIRouter(prefix="/api/contracts", tags=["合同"])


# ---------------------------------------------------------------- 工具
def _to_out(c: Contract, risk_level: str = "none", finding_count: int = 0) -> ContractOut:
    """把 ORM 对象转成响应模型。

    最新审查的风险等级/意见数由调用方传进来 —— 列表页会一次性批量查出
    整页合同的最新审查，避免「每份合同一次 SQL」的 N+1 查询。
    """
    return ContractOut(
        id=c.id,
        title=c.title,
        original_filename=c.original_filename,
        file_size=c.file_size,
        contract_type=c.contract_type,
        char_count=c.char_count,
        status=c.status,
        submitter_id=c.submitter_id,
        dept_id=c.dept_id,
        created_at=c.created_at,
        updated_at=c.updated_at,
        submitter_name=c.submitter.display_name if c.submitter else "",
        dept_name=c.department.name if c.department else "",
        risk_level=risk_level,
        finding_count=finding_count,
    )


def _is_visible(contract: Contract, user: User) -> bool:
    """合同可见性判定（与 security.contract_visibility_filter 同一套规则）。"""
    # 管理员不读正文（职责分离）。两处判定必须同步，否则列表和详情会不一致。
    if user.role == "admin":
        return False
    if user.role in ("reviewer", "finance"):
        return True
    if contract.submitter_id == user.id:
        return True
    return user.dept_id is not None and contract.dept_id == user.dept_id


def _get_visible_contract(db: Session, contract_id: int, user: User) -> Contract:
    """取合同 + 检查可见性。越权统一返回 404，不泄漏"存在但你没权限"。"""
    contract = db.get(Contract, contract_id)
    if contract is None or not _is_visible(contract, user):
        raise HTTPException(status_code=404, detail="合同不存在")
    return contract


def _find_duplicate(db: Session, digest: str, user: User) -> Contract | None:
    """按内容哈希找已存在的合同。

    只在「当前用户本来就有权看」时才返回 —— 否则去重会变成跨部门的信息泄露。
    """
    same = db.scalars(
        select(Contract).where(Contract.content_hash == digest).order_by(Contract.id)
    ).all()
    return next((c for c in same if _is_visible(c, user)), None)


def _visible_clauses(contract: Contract, user: User) -> list[Clause]:
    """按角色过滤条款。财务看不到 liability/termination 等。"""
    return [c for c in contract.clauses if clause_visible(c.visibility, user)]


# ---------------------------------------------------------------- 上传
@router.post("/upload", response_model=ContractDetail, status_code=status.HTTP_201_CREATED)
async def upload_contract(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_roles("submitter", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="合同文件 pdf/docx/txt")],
    title: Annotated[str, Form()] = "",
    contract_type: Annotated[str, Form()] = "其他",
):
    """
    上传合同文件。解析失败直接拒单（宁可拒，不要错位）。
    """
    if user.role not in ("submitter", "reviewer", "admin"):
        raise HTTPException(status_code=403, detail="当前角色无权上传合同")

    filename = file.filename or "unnamed"
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in config.ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式 {suffix}，仅支持 {', '.join(sorted(config.ALLOWED_EXT))}",
        )

    # 分块读取：不能把 20MB 整份先读进内存再检查大小，
    # 否则一个超大文件就能把进程内存拉满。
    limit_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过上限 {config.MAX_UPLOAD_MB} MB，请压缩或拆分后上传",
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    digest = content_hash(data)

    # 内容去重：同一份合同反复上传没有任何价值，只会制造噪音
    duplicate = _find_duplicate(db, digest, user)
    if duplicate is not None:
        response.status_code = status.HTTP_200_OK
        return get_contract(duplicate.id, user, db).model_copy(update={"duplicate": True})

    # 存盘
    safe_name = f"{digest[:16]}{suffix}"
    stored = config.UPLOAD_DIR / safe_name
    stored.write_bytes(data)

    # 解析
    parsed = parse_contract(stored, config.MAX_CONTRACT_CHARS)
    if not parsed.ok:
        stored.unlink(missing_ok=True)
        write_audit(
            db, user=user, action="contract_parse_failed", target_type="contract",
            detail={"filename": filename, "reason": parsed.error},
            request=request, success=False,
        )
        raise HTTPException(
            status_code=422,
            detail=f"合同解析未通过自检，已拒绝提交：{parsed.error}",
        )

    contract = Contract(
        title=title.strip() or filename.rsplit(".", 1)[0],
        original_filename=filename,
        stored_path=str(stored),
        file_size=len(data),
        content_hash=digest,
        contract_type=contract_type or "其他",
        raw_text=parsed.text,
        char_count=len(parsed.text),
        status="uploaded",
        submitter_id=user.id,
        dept_id=user.dept_id,
    )
    db.add(contract)
    db.flush()  # 拿到 id

    for pc in parsed.clauses:
        db.add(
            Clause(
                contract_id=contract.id,
                order_no=pc.order_no,
                heading=pc.heading,
                content=pc.content,
                category=pc.category,
                visibility=pc.visibility,
            )
        )
    db.commit()
    db.refresh(contract)

    write_audit(
        db, user=user, action="contract_upload", target_type="contract",
        target_id=contract.id,
        detail={
            "filename": filename,
            "char_count": len(parsed.text),
            "clause_count": len(parsed.clauses),
            "quality": parsed.quality,
        },
        request=request,
    )
    return get_contract(contract.id, user, db)


@router.post("/text", response_model=ContractDetail, status_code=status.HTTP_201_CREATED)
def create_from_text(
    payload: ContractCreateText,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_roles("submitter", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
):
    """直接粘贴文本创建合同 —— 演示和评测跑分都走这条路，比上传文件快。"""
    if user.role not in ("submitter", "reviewer", "admin"):
        raise HTTPException(status_code=403, detail="当前角色无权创建合同")

    from pathlib import Path

    digest = content_hash(payload.content.encode("utf-8"))
    duplicate = _find_duplicate(db, digest, user)
    if duplicate is not None:
        response.status_code = status.HTTP_200_OK
        return get_contract(duplicate.id, user, db).model_copy(update={"duplicate": True})

    safe_name = f"{digest[:16]}.txt"
    stored = config.UPLOAD_DIR / safe_name
    stored.write_text(payload.content, encoding="utf-8")

    parsed = parse_contract(stored, config.MAX_CONTRACT_CHARS)
    if not parsed.ok:
        # 解析失败要清掉刚落盘的文件，否则 uploads 里会堆积没人引用的垃圾文件。
        # 同一份内容可能已被别的合同引用（按内容哈希命名），所以先确认没人用。
        in_use = db.scalars(
            select(Contract.id).where(Contract.stored_path == str(stored))
        ).first()
        if in_use is None:
            stored.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail=f"合同解析未通过自检：{parsed.error}",
        )

    contract = Contract(
        title=payload.title,
        original_filename=f"{payload.title}.txt",
        stored_path=str(stored),
        file_size=len(payload.content.encode("utf-8")),
        content_hash=digest,
        contract_type=payload.contract_type,
        raw_text=parsed.text,
        char_count=len(parsed.text),
        status="uploaded",
        submitter_id=user.id,
        dept_id=user.dept_id,
    )
    db.add(contract)
    db.flush()

    for pc in parsed.clauses:
        db.add(
            Clause(
                contract_id=contract.id,
                order_no=pc.order_no,
                heading=pc.heading,
                content=pc.content,
                category=pc.category,
                visibility=pc.visibility,
            )
        )
    db.commit()
    db.refresh(contract)

    write_audit(
        db, user=user, action="contract_create_text", target_type="contract",
        target_id=contract.id,
        detail={"title": payload.title, "clause_count": len(parsed.clauses)},
        request=request,
    )
    return get_contract(contract.id, user, db)


from typing import Annotated
from fastapi import Query

# ---------------------------------------------------------------- 列表
@router.get("", response_model=dict)
def list_contracts(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    status_filter: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    """
    合同列表（分页）。

    关键：过滤条件在 SQL 里。
    submitter 查不到别的部门的合同 —— 不是查出来藏起来，是根本没查。
    """
    from sqlalchemy import func

    stmt = select(Contract).options(
        selectinload(Contract.submitter), selectinload(Contract.department)
    )

    cond = contract_visibility_filter(user)
    if cond is not True:
        stmt = stmt.where(cond)

    if status_filter:
        stmt = stmt.where(Contract.status == status_filter)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(Contract.title.like(like), Contract.original_filename.like(like)))

    # 总数
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    stmt = stmt.order_by(Contract.created_at.desc()).offset((page - 1) * size).limit(size)
    contracts = db.scalars(stmt).all()

    # 一次查出整页合同各自的最新审查（按 (contract_id, max(review_id)) 分组），
    # 而不是每份合同回头再查一次 Review。
    stats: dict[int, tuple[str, int]] = {}
    ids = [c.id for c in contracts]
    if ids:
        latest_ids = (
            select(Review.contract_id, func.max(Review.id).label("latest_id"))
            .where(Review.contract_id.in_(ids))
            .group_by(Review.contract_id)
            .subquery()
        )
        rows = db.execute(
            select(Review.contract_id, Review.risk_level, Review.findings).join(
                latest_ids, Review.id == latest_ids.c.latest_id
            )
        ).all()
        for cid, level, findings in rows:
            stats[cid] = (level, len(findings or []))

    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [_to_out(c, *stats.get(c.id, ("none", 0))) for c in contracts],
    }


# ---------------------------------------------------------------- 详情
@router.get("/{contract_id}", response_model=ContractDetail)
def get_contract(
    contract_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """
    合同详情。

    这里返回的 clauses 和 findings 都已经按角色过滤过 ——
    财务打开合同时，违约责任那几条既不在条款里，也不在 AI 意见里。
    """
    contract = _get_visible_contract(db, contract_id, user)

    visible = _visible_clauses(contract, user)
    visible_nos = {c.order_no for c in visible}

    latest = db.scalars(
        select(Review).where(Review.contract_id == contract.id).order_by(Review.id.desc())
    ).first()

    latest_out = None
    if latest:
        filtered_findings = [
            f for f in (latest.findings or []) if f.get("clause_no") in visible_nos
        ]
        # 条款级反馈也要按可见条款过滤，别把隐藏条款的反馈透给财务
        edits = dict(latest.reviewer_edits or {})
        if edits.get("clause_feedbacks"):
            edits["clause_feedbacks"] = [
                fb for fb in edits["clause_feedbacks"] if fb.get("clause_no") in visible_nos
            ]

        latest_out = ReviewOut(
            id=latest.id,
            contract_id=latest.contract_id,
            prompt_version_id=latest.prompt_version_id,
            model_name=latest.model_name,
            findings=filtered_findings,
            risk_level=latest.risk_level,
            summary=latest.summary,
            degraded=latest.degraded,
            degrade_reason=latest.degrade_reason,
            prompt_tokens=latest.prompt_tokens,
            completion_tokens=latest.completion_tokens,
            total_tokens=latest.total_tokens,
            latency_ms=latest.latency_ms,
            cost_cny=latest.cost_cny,
            reviewer_id=latest.reviewer_id,
            reviewer_action=latest.reviewer_action,
            reviewer_note=latest.reviewer_note,
            reviewer_edits=edits or None,
            created_at=latest.created_at,
            reviewed_at=latest.reviewed_at,
        )

    base = _to_out(
        contract,
        latest.risk_level if latest else "none",
        len(latest.findings or []) if latest else 0,
    )
    return ContractDetail(
        **base.model_dump(),
        clause_total=len(contract.clauses),
        clauses=[
            ClauseOut(
                id=c.id,
                order_no=c.order_no,
                heading=c.heading,
                content=c.content,
                category=c.category,
                visibility=c.visibility,
            )
            for c in visible
        ],
        latest_review=latest_out,
    )


# ---------------------------------------------------------------- 审查
@router.post("/{contract_id}/review", response_model=ReviewOut)
def run_review(
    contract_id: int,
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    prompt_version_id: int | None = None,
):
    """
    触发审查。

    成本护栏在这里生效：
    - 每个用户每天最多 N 份
    - 单份 token 上限（在 review_contract 里检查）
    """
    # 审查是法务的职责：提交人负责上传，管理员只看统计（也读不到正文）。
    # 原来这里放行了 submitter，等于上传的人能自己给自己出结论，审计上也说不清。
    if user.role != "reviewer":
        raise HTTPException(status_code=403, detail="只有法务审查员可以发起审查")

    contract = _get_visible_contract(db, contract_id, user)

    # 每日配额
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=1)
    # 配额必须按「人」算。Review 上没有发起人字段（reviewer_id 记的是反馈人），
    # 所以用审计日志里该用户的 contract_review 记录来计数。
    # 原来统计的是全库 Review，只要全公司当天跑满 50 次，所有人都被锁死。
    today_count = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.user_id == user.id,
            AuditLog.action == "contract_review",
            AuditLog.created_at >= since,
        )
    ) or 0
    if today_count >= config.MAX_CONTRACTS_PER_USER_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=(
                f"今日审查次数已达上限（{config.MAX_CONTRACTS_PER_USER_PER_DAY} 次），"
                "请明天再试或联系管理员调整配额"
            ),
        )

    visible = _visible_clauses(contract, user)
    if not visible:
        raise HTTPException(status_code=400, detail="没有你可查看的条款，无法审查")

    pv = None
    if prompt_version_id:
        from ..models import PromptVersion

        pv = db.get(PromptVersion, prompt_version_id)
        if pv is None:
            raise HTTPException(status_code=404, detail="指定的 prompt 版本不存在")

    try:
        outcome = review_contract(db, contract, visible_clauses=visible, prompt_version=pv)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    review = save_review(db, contract, outcome)

    write_audit(
        db, user=user, action="contract_review", target_type="contract",
        target_id=contract.id,
        detail={
            "review_id": review.id,
            "model": outcome.model_name,
            "degraded": outcome.degraded,
            "findings": len(outcome.findings),
            "tokens": outcome.total_tokens,
            "cost_cny": outcome.cost_cny,
            "latency_ms": outcome.latency_ms,
            "prompt_version_id": outcome.prompt_version_id,
        },
        request=request,
    )

    return ReviewOut.model_validate(review)


# ---------------------------------------------------------------- 反馈（整体）
@router.post("/{contract_id}/review/{review_id}/feedback", response_model=ReviewOut)
def submit_feedback(
    contract_id: int,
    review_id: int,
    payload: ReviewFeedback,
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """
    法务对 AI 意见的反馈 —— 数据飞轮的入口。

    这里写入的每一条 accepted/edited/rejected，
    将来都是构造评测集和微调数据的原料。
    """
    if user.role not in ("reviewer", "admin"):
        raise HTTPException(status_code=403, detail="仅法务审查员和管理员可提交审查意见反馈")

    review = db.get(Review, review_id)
    if review is None or review.contract_id != contract_id:
        raise HTTPException(status_code=404, detail="审查记录不存在")

    from datetime import datetime, timezone

    review.reviewer_id = user.id
    review.reviewer_action = payload.action
    review.reviewer_note = payload.note
    review.reviewer_edits = payload.edits
    review.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(review)

    write_audit(
        db, user=user, action=f"review_feedback_{payload.action}",
        target_type="review", target_id=review.id,
        detail={"contract_id": contract_id, "note": payload.note},
        request=request,
    )
    return ReviewOut.model_validate(review)


# ---------------------------------------------------------------- 反馈（条款级）
from pydantic import BaseModel

class ClauseFeedback(BaseModel):
    clause_no: int
    # 用 Literal 而不是 str：非法动作由 FastAPI 直接 422，不会把脏数据写进 reviewer_edits
    action: Literal["accepted", "edited", "rejected"]
    note: str | None = None

@router.post("/{contract_id}/review/{review_id}/clause-feedback", response_model=ReviewOut)
def submit_clause_feedback(
    contract_id: int,
    review_id: int,
    payload: ClauseFeedback,
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """
    法务对特定条款的 AI 意见进行反馈。
    
    这是更细粒度的数据飞轮入口，允许针对单个条款的 AI 意见提交反馈。
    """
    if user.role not in ("reviewer", "admin"):
        raise HTTPException(status_code=403, detail="仅法务审查员和管理员可提交条款级反馈")

    review = db.get(Review, review_id)
    if review is None or review.contract_id != contract_id:
        raise HTTPException(status_code=404, detail="审查记录不存在")

    # 验证条款号是否在可见范围内
    contract = _get_visible_contract(db, contract_id, user)
    visible = _visible_clauses(contract, user)
    visible_nos = {c.order_no for c in visible}
    if payload.clause_no not in visible_nos:
        # 条款不存在 / 对你不可见 统一 404，不区以区别，免泄露条款是否存在
        raise HTTPException(status_code=404, detail="条款不存在或对你不可见")

    from datetime import datetime, timezone

    # 记录条款级反馈到审查记录的 reviewer_edits 字段（扩展结构）
    clause_feedback = {
        "clause_no": payload.clause_no,
        "action": payload.action,
        "note": payload.note,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    # 合并到现有 reviewer_edits
    existing_edits = review.reviewer_edits or {}
    clause_feedbacks = existing_edits.get("clause_feedbacks", [])
    clause_feedbacks.append(clause_feedback)
    existing_edits["clause_feedbacks"] = clause_feedbacks
    review.reviewer_edits = existing_edits
    
    review.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(review)

    write_audit(
        db, user=user, action=f"review_clause_feedback_{payload.action}",
        target_type="review", target_id=review.id,
        detail={"contract_id": contract_id, "clause_no": payload.clause_no, "note": payload.note},
        request=request,
    )
    return ReviewOut.model_validate(review)


@router.get("/{contract_id}/reviews", response_model=list[ReviewOut])
def list_reviews(
    contract_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """一份合同的所有历史审查记录（能看出改了 prompt 之后的变化）。"""
    contract = _get_visible_contract(db, contract_id, user)
    reviews = db.scalars(
        select(Review).where(Review.contract_id == contract.id).order_by(Review.id.desc())
    ).all()
    return [ReviewOut.model_validate(r) for r in reviews]
