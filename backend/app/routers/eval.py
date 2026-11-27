# ==========================================================
#  评测路由
# ----------------------------------------------------------
#  管理员可以：
#  - 初始化评测集
#  - 跑一次评测（用指定 prompt 版本 / 规则引擎基线）
#  - 查看历史跑分，对比分数变化
# ==========================================================
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..database import SessionLocal, get_db
from ..eval_runner import run_eval, seed_eval_cases
from ..models import EvalCase, EvalRun, PromptVersion, User
from ..schemas import EvalCaseOut, EvalRunOut
from ..security import CurrentUser, require_roles, write_audit

router = APIRouter(prefix="/api/eval", tags=["评测"])


@router.get("/cases", response_model=list[EvalCaseOut])
def list_cases(
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
):
    cases = db.scalars(select(EvalCase).order_by(EvalCase.id)).all()
    return [
        EvalCaseOut(
            id=c.id,
            case_id=c.case_id,
            contract_type=c.contract_type,
            title=c.title,
            planted_count=len(c.planted_risks or []),
            normal_clause_count=c.normal_clause_count,
        )
        for c in cases
    ]


@router.post("/seed")
def seed(
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
):
    """把内置的 20 份评测用例写入数据库（幂等，重复调用不会重复插入）。"""
    added = seed_eval_cases(db)
    write_audit(
        db, user=user, action="eval_seed", target_type="eval",
        detail={"added": added}, request=request,
    )
    total = db.query(EvalCase).count()
    return {"ok": True, "added": added, "total": total}


def _run_eval_job(
    run_id: int,
    prompt_version_id: int | None,
    label: str,
    limit: int | None,
    rules_only: bool,
    user_id: int,
) -> None:
    """
    后台跑评测。必须自己开 session —— 请求的 session 在响应返回后就关闭了。
    """
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        if run is None:
            return
        pv = db.get(PromptVersion, prompt_version_id) if prompt_version_id else None
        run_eval(
            db,
            prompt_version=pv,
            label=label,
            limit=limit,
            use_rules_only=rules_only,
            run=run,
        )
        write_audit(
            db, user=db.get(User, user_id), action="eval_run", target_type="eval_run",
            target_id=run.id,
            detail={
                "label": run.label,
                "recall": run.recall,
                "fpr": run.false_positive_rate,
                "f1": run.f1,
                "cases": run.total_cases,
            },
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        # 失败必须落痕，否则前端会一直转圈等一个永远不会来的结果
        failed = db.get(EvalRun, run_id)
        if failed is not None:
            failed.detail = {"status": "failed", "error": str(exc)[:300]}
            db.commit()
    finally:
        db.close()


@router.post("/run", response_model=EvalRunOut)
def run(
    background: BackgroundTasks,
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
    prompt_version_id: int | None = None,
    limit: int | None = Query(default=None, ge=1, le=20),
    rules_only: bool = False,
    label: str = "",
):
    """
    提交一次评测，立即返回。

    20 份合同要真实调用模型，耗时 1-3 分钟。同步等待会一直占着连接，
    浏览器也可能超时拿不到结果。这里先落一条 running 记录，后台任务跑完再更新，
    前端轮询 /api/eval/runs 看进度。想快速验证可以传 limit=3。
    """
    pv = None
    if prompt_version_id:
        pv = db.get(PromptVersion, prompt_version_id)
        if pv is None:
            raise HTTPException(status_code=404, detail="prompt 版本不存在")

    placeholder = EvalRun(
        prompt_version_id=pv.id if pv else None,
        model_name="规则引擎" if rules_only else config.LLM_MODEL,
        label=label or ("规则引擎基线" if rules_only else "AI 审查"),
        total_cases=0,
        detail={"status": "running", "started_at": datetime.now(timezone.utc).isoformat()},
    )
    db.add(placeholder)
    db.commit()
    db.refresh(placeholder)

    background.add_task(
        _run_eval_job,
        placeholder.id,
        pv.id if pv else None,
        label,
        limit,
        rules_only,
        user.id,
    )
    return EvalRunOut.model_validate(placeholder)


from datetime import datetime, timezone
from fastapi.responses import StreamingResponse
import csv
import io


@router.get("/runs", response_model=list[EvalRunOut])
def list_runs(
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
):
    """获取评测历史跑分列表。"""
    runs = db.scalars(select(EvalRun).order_by(EvalRun.id.desc())).all()
    return [EvalRunOut.model_validate(r) for r in runs]


@router.get("/runs/export")
def export_eval(
    user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    db: Annotated[Session, Depends(get_db)],
):
    """导出评测结果 CSV（历史跑分汇总 + 最新一次逐用例明细）。"""
    runs = db.scalars(select(EvalRun).order_by(EvalRun.id.desc())).all()
    if not runs:
        raise HTTPException(status_code=404, detail="暂无评测记录")

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        
        # 历史跑分汇总
        writer.writerow(["=== 历史跑分汇总 ==="])
        writer.writerow(["#", "标签", "模型", "抓到/埋雷", "召回率", "误报率", "F1", "平均耗时(ms)", "时间"])
        for r in runs:
            writer.writerow([
                r.id, r.label, r.model_name, f"{r.caught_total}/{r.planted_total}",
                f"{r.recall*100:.1f}%", f"{r.false_positive_rate*100:.1f}%",
                f"{r.f1*100:.1f}%", r.avg_latency_ms,
                r.created_at.isoformat() if r.created_at else ""
            ])
        
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        
        writer.writerow([])
        writer.writerow(["=== 最新一次逐用例明细 ==="])
        writer.writerow(["用例", "标题", "抓到/埋雷", "报出", "误报", "结果", "漏报条款"])
        
        latest = runs[0]
        detail = latest.detail or {}
        for c in (detail.get('cases', []) or []):
            writer.writerow([
                c.get('case_id', ''), c.get('title', ''), f"{c.get('caught', 0)}/{c.get('planted', 0)}", c.get('findings', 0), c.get('false_positives', 0),
                "全抓到" if c.get('caught', 0) >= c.get('planted', 0) else f"漏 {len(c.get('missed', []) or [])}",
                ", ".join(c.get('missed', []) or []) or "—"
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="eval-results-{datetime.now(timezone.utc).date().isoformat()}.csv"'},
    )


@router.get("/runs/{run_id}", response_model=EvalRunOut)
def get_run(
    run_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    r = db.get(EvalRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="评测记录不存在")
    return EvalRunOut.model_validate(r)
