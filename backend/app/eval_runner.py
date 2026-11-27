# ==========================================================
#  评测跑分器
# ----------------------------------------------------------
#  跑分逻辑：
#  对每个用例：
#    1. 用当前生效的 prompt 跑一次审查
#    2. 对每个埋进去的风险点，检查模型是否报出了「同一个条款号 + 同类别」的意见
#       且意见文本里出现了该风险点的任一关键词 → 算抓到
#    3. 模型报出但不在标准答案里的意见 → 算误报
#
#  指标：
#  - 召回率 recall = 抓到的雷 / 埋的雷总数      ← 最重要
#  - 误报率 fpr   = 误报条数 / 正常条款总数
#  - F1           = 2PR/(P+R)
#
#  为什么要用关键词宽松匹配而不是严格文本比对：
#  模型说"违约金比例过高，达合同总额5%"和"逾期违约金按5%计，明显偏高"
#  意思一样但字面不同，严格比对会把正确的判成漏报。
# ==========================================================
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .eval_dataset import EvalCaseData, build_dataset
from .models import Contract, EvalCase, EvalRun, PromptVersion, Review, User
from .parser import split_clauses
from .review_service import normalize_findings, overall_risk, review_contract
from .config import config


@dataclass
class CaseScore:
    case_id: str
    title: str
    planted: int
    caught: int
    missed: list[str] = field(default_factory=list)
    false_positives: int = 0
    findings: int = 0
    degraded: bool = False
    latency_ms: int = 0


@dataclass
class RunScore:
    total_cases: int
    planted_total: int
    caught_total: int
    false_positive_total: int
    normal_clause_total: int
    recall: float
    false_positive_rate: float
    precision: float
    f1: float
    avg_latency_ms: int
    total_tokens: int
    cases: list[CaseScore] = field(default_factory=list)


def seed_eval_cases(db: Session) -> int:
    """把内置评测集写进数据库（幂等）。"""
    existing = {c.case_id for c in db.query(EvalCase).all()}
    added = 0
    for data in build_dataset():
        if data.case_id in existing:
            continue
        db.add(
            EvalCase(
                case_id=data.case_id,
                contract_type=data.contract_type,
                title=data.title,
                content=data.content,
                planted_risks=[
                    {
                        "clause_no": r.clause_no,
                        "category": r.category,
                        "level": r.level,
                        "keywords": r.keywords,
                    }
                    for r in data.risks
                ],
                normal_clause_count=data.normal_clause_count,
            )
        )
        added += 1
    db.commit()
    return added


def score_case(data: EvalCaseData, findings: list[dict]) -> CaseScore:
    """
    给单个用例打分。

    匹配规则（宽松但合理）：
    一条埋的雷算「抓到」，必须同时满足：
      1. 模型报的 clause_no 与埋的条款号相同
      2. 模型报的 category 与埋的类别相同，或者意见文本里含该风险点的关键词
    """
    caught = 0
    missed: list[str] = []
    matched_finding_idx: set[int] = set()

    for risk in data.risks:
        hit = False
        for idx, f in enumerate(findings):
            if f.get("clause_no") != risk.clause_no:
                continue
            blob = f"{f.get('issue', '')} {f.get('suggestion', '')}"
            kw_hit = any(kw in blob for kw in risk.keywords)
            cat_hit = f.get("category") == risk.category
            if kw_hit or cat_hit:
                hit = True
                matched_finding_idx.add(idx)
                break
        if hit:
            caught += 1
        else:
            missed.append(f"第{risk.clause_no}条/{risk.category}")

    # 误报 = 模型报的、但没匹配上任何标准答案的
    false_positives = len(findings) - len(matched_finding_idx)
    # 一份干净的合同，模型只要报了任何一条就是误报
    if not data.risks:
        false_positives = len(findings)

    return CaseScore(
        case_id=data.case_id,
        title=data.title,
        planted=len(data.risks),
        caught=caught,
        missed=missed,
        false_positives=max(0, false_positives),
        findings=len(findings),
    )


def run_eval(
    db: Session,
    *,
    prompt_version: PromptVersion | None = None,
    label: str = "",
    limit: int | None = None,
    use_rules_only: bool = False,
    run: EvalRun | None = None,
) -> EvalRun:
    """
    跑一次完整评测。

    use_rules_only=True 时不调模型，只跑规则引擎 ——
    用来做「降级模式」的基线对比，也能在没网/没额度时演示。

    run: 传入则写入这条已有记录（后台任务先建了 running 占位行），
    否则新建一条。
    """
    cases = build_dataset()
    if limit:
        cases = cases[:limit]

    from .models import Clause as ClauseModel
    from .review_service import rule_based_review

    scores: list[CaseScore] = []
    planted_total = 0
    caught_total = 0
    fp_total = 0
    normal_total = 0
    token_total = 0
    latency_total = 0

    started = time.perf_counter()

    for data in cases:
        # 用真实的解析器切分，保证和线上路径一致
        parsed_clauses = split_clauses(data.content)

        if use_rules_only:
            # 造一个临时的 clause 对象列表给规则引擎用
            class _C:
                def __init__(self, pc):
                    self.order_no = pc.order_no
                    self.heading = pc.heading
                    self.content = pc.content

            findings = rule_based_review([_C(pc) for pc in parsed_clauses])
            latency = 0
            tokens = 0
            degraded = True
        else:
            # 直接调 review_contract 会需要 Contract 对象，这里构造一个轻量的
            class _Contract:
                title = data.title
                contract_type = data.contract_type

            class _Clause:
                def __init__(self, pc):
                    self.order_no = pc.order_no
                    self.heading = pc.heading
                    self.content = pc.content
                    self.category = pc.category
                    self.visibility = pc.visibility

            outcome = review_contract(
                db,
                _Contract(),  # type: ignore[arg-type]
                visible_clauses=[_Clause(pc) for pc in parsed_clauses],
                prompt_version=prompt_version,
            )
            findings = outcome.findings
            latency = outcome.latency_ms
            tokens = outcome.total_tokens
            degraded = outcome.degraded

        score = score_case(data, findings)
        score.latency_ms = latency
        score.degraded = degraded
        scores.append(score)

        planted_total += score.planted
        caught_total += score.caught
        fp_total += score.false_positives
        normal_total += data.normal_clause_count
        token_total += tokens
        latency_total += latency

    recall = caught_total / planted_total if planted_total else 0.0
    fpr = fp_total / normal_total if normal_total else 0.0
    precision = caught_total / (caught_total + fp_total) if (caught_total + fp_total) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    )

    payload = {
        "prompt_version_id": prompt_version.id if prompt_version else None,
        "model_name": "规则引擎" if use_rules_only else config.LLM_MODEL,
        "label": label or ("规则引擎基线" if use_rules_only else "AI 审查"),
        "total_cases": len(cases),
        "planted_total": planted_total,
        "caught_total": caught_total,
        "false_positive_total": fp_total,
        "recall": round(recall, 4),
        "false_positive_rate": round(fpr, 4),
        "f1": round(f1, 4),
        "avg_latency_ms": int(latency_total / len(cases)) if cases else 0,
        "total_tokens": token_total,
        "detail": {
            "status": "done",
            "precision": round(precision, 4),
            "normal_clause_total": normal_total,
            "elapsed_sec": round(time.perf_counter() - started, 1),
            "cases": [
                {
                    "case_id": s.case_id,
                    "title": s.title,
                    "planted": s.planted,
                    "caught": s.caught,
                    "missed": s.missed,
                    "false_positives": s.false_positives,
                    "findings": s.findings,
                    "degraded": s.degraded,
                    "latency_ms": s.latency_ms,
                }
                for s in scores
            ],
        },
    }
    if run is None:
        run = EvalRun(**payload)
        db.add(run)
    else:
        for key, value in payload.items():
            setattr(run, key, value)
    db.commit()
    db.refresh(run)
    return run
