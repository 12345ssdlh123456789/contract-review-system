# ==========================================================
#  审查服务 —— 项目的心脏
# ----------------------------------------------------------
#  流程：取 prompt 版本 → 拼上下文 → 调模型 → 解析 JSON →
#        校验条款号 → 失败则降级到规则引擎 → 落库
#
#  三个企业级要点：
#  1. 降级链：模型挂了不能整个功能不可用，要有规则引擎兜底
#  2. 结果校验：模型会编造不存在的条款号，必须过滤
#  3. 全量记录：每次调用的 token/耗时/成本都要落库
# ==========================================================
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .config import config
from .models import Clause, Contract, PromptVersion, Review
from .parser import classify_clause
from .prompts import render_clauses_text


@dataclass
class ReviewOutcome:
    findings: list[dict]
    summary: str
    risk_level: str
    degraded: bool
    degrade_reason: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    cost_cny: float
    model_name: str
    prompt_version_id: int | None


# ---------------------------------------------------------------- 规则引擎
# 模型不可用时的兜底。准确率不如 LLM，但能抓到最硬的几类问题，
# 而且永远不会超时、不会花钱。
RULE_RULES: list[dict] = [
    {
        "name": "账期过长",
        "category": "payment",
        "level": "high",
        "pattern": r"(?:账期|付款期限|支付期限)[^。；\n]{0,20}?(\d{2,3})\s*(?:个)?(?:日|天)",
        "check": lambda m: int(m.group(1)) > 90,
        "issue": "付款账期超过 90 天，资金占用成本高且回款风险大",
        "suggestion": "建议将账期压缩至 60 天以内，或约定分期付款并挂钩验收节点",
    },
    {
        "name": "预付比例过高",
        "category": "payment",
        "level": "medium",
        "pattern": r"(?:预付|首付|定金)[^。；\n]{0,20}?(\d{2,3})\s*%",
        "check": lambda m: int(m.group(1)) > 50,
        "issue": "预付款比例超过 50%，一旦对方履约不力，追回资金困难",
        "suggestion": "建议预付款不超过 30%，其余款项与交付验收节点挂钩",
    },
    {
        "name": "单方解除权",
        "category": "termination",
        "level": "high",
        "pattern": r"(?:乙方|甲方|对方|供方)[^。；\n]{0,15}(?:有权|可以)[^。；\n]{0,10}(?:随时|任意|单方)[^。；\n]{0,10}(?:解除|终止)",
        "check": lambda m: True,
        "issue": "存在单方任意解除权，且未约定对等权利或补偿机制",
        "suggestion": "建议改为双向对等解除权，并约定提前通知期及已发生费用的结算方式",
    },
    {
        "name": "连带责任无上限",
        "category": "liability",
        "level": "high",
        "pattern": r"连带(?:责任|赔偿)",
        "check": lambda m: True,
        "issue": "约定连带责任但未设赔偿上限，风险敞口不可控",
        "suggestion": "建议增加责任限额条款，将累计赔偿上限限定为合同总额的一定比例",
    },
    {
        "name": "异地管辖",
        "category": "dispute",
        "level": "medium",
        "pattern": r"(?:由|向)[^。；\n]{0,10}(?:人民法院|法院|仲裁委员会)[^。；\n]{0,10}(?:管辖|仲裁)",
        "check": lambda m: True,
        "issue": "争议解决地约定为对方所在地或异地，维权成本显著增加",
        "suggestion": "建议改为我方所在地有管辖权的法院，或约定双方认可的仲裁机构",
    },
    {
        "name": "无限保密期限",
        "category": "confidentiality",
        "level": "low",
        "pattern": r"保密(?:义务|期限)[^。；\n]{0,15}(?:永久|无限期|长期有效)",
        "check": lambda m: True,
        "issue": "保密义务约定为永久或无限期，超出合理范围",
        "suggestion": "建议将保密期限限定为合同终止后 3 至 5 年",
    },
    {
        "name": "自动续约无退出",
        "category": "termination",
        "level": "medium",
        "pattern": r"自动(?:续约|续期|延长)",
        "check": lambda m: True,
        "issue": "约定自动续约但未明确退出机制与提前通知要求",
        "suggestion": "建议明确续约前需书面确认，并约定不续约的提前通知期限",
    },
    {
        "name": "违约金比例过高",
        "category": "liability",
        "level": "medium",
        "pattern": r"违约金[^。；\n]{0,15}?(\d{2,3})\s*%",
        "check": lambda m: int(m.group(1)) > 30,
        "issue": "违约金比例过高，可能超出实际损失范围",
        "suggestion": "建议将违约金比例控制在合同总额的 20% 以内，并与实际损失挂钩",
    },
]


def rule_based_review(clauses: list[Clause]) -> list[dict]:
    """纯规则审查。条款里命中规则就生成一条意见。"""
    findings: list[dict] = []
    for clause in clauses:
        blob = f"{clause.heading}\n{clause.content}"
        for rule in RULE_RULES:
            m = re.search(rule["pattern"], blob)
            if not m:
                continue
            try:
                if not rule["check"](m):
                    continue
            except (ValueError, IndexError):
                continue
            findings.append(
                {
                    "clause_no": clause.order_no,
                    "level": rule["level"],
                    "category": rule["category"],
                    "issue": rule["issue"],
                    "suggestion": rule["suggestion"],
                    "confidence": 0.6,  # 规则引擎置信度天然低
                    "source": "rule",
                }
            )
    return findings


# ---------------------------------------------------------------- 结果校验
VALID_LEVELS = {"high", "medium", "low"}
VALID_CATEGORIES = {
    "payment", "liability", "termination", "confidentiality",
    "dispute", "delivery", "ip", "other",
}

# 模型经常把类别写成中文，必须映射回来。
# 不做这一步的话，所有中文类别都会掉进 other，评测时 category 匹配全失效。
CATEGORY_ALIASES = {
    "付款": "payment", "支付": "payment", "价款": "payment", "费用": "payment",
    "付款风险": "payment", "资金": "payment", "结算": "payment",
    "违约": "liability", "责任": "liability", "赔偿": "liability",
    "违约责任": "liability", "责任不对等": "liability", "风险责任": "liability",
    "解除": "termination", "终止": "termination", "解除权": "termination",
    "解除权失衡": "termination", "续约": "termination",
    "保密": "confidentiality", "保密义务": "confidentiality", "商业秘密": "confidentiality",
    "争议": "dispute", "争议解决": "dispute", "管辖": "dispute", "仲裁": "dispute",
    "交付": "delivery", "验收": "delivery", "工期": "delivery", "交付与验收": "delivery",
    "知识产权": "ip", "成果归属": "ip", "ip归属": "ip",
    "其他": "other", "缺失条款": "other",
}


def normalize_findings(raw: list, clauses: list[Clause]) -> list[dict]:
    """
    清洗模型输出。

    模型最常见的三种毛病：
    1. 编造不存在的条款号（比如合同只有 12 条，它说第 15 条）
    2. level 写成中文"高"/"中"
    3. 缺字段
    这里全部规范化，坏数据直接丢弃 —— 宁可少一条，不要错一条。
    """
    valid_nos = {c.order_no for c in clauses}
    level_map = {"高": "high", "中": "medium", "低": "low", "严重": "high", "一般": "medium"}

    cleaned: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        # 条款号
        try:
            no = int(item.get("clause_no", 0))
        except (TypeError, ValueError):
            no = 0
        if no not in valid_nos:
            # 编造的条款号 —— 丢弃，但记一笔
            continue

        level = str(item.get("level", "")).strip().lower()
        level = level_map.get(level, level)
        if level not in VALID_LEVELS:
            level = "medium"

        category = str(item.get("category", "")).strip()
        # 中文类别映射回英文枚举，否则评测的 category 匹配全部失效
        if category in CATEGORY_ALIASES:
            category = CATEGORY_ALIASES[category]
        category = category.lower()
        if category not in VALID_CATEGORIES:
            # 类别不认识时，用条款文本自己重新判断一次，比直接归 other 准
            matched_clause = next((c for c in clauses if c.order_no == no), None)
            category = (
                classify_clause(matched_clause.heading, matched_clause.content)
                if matched_clause
                else "other"
            )

        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue

        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))

        cleaned.append(
            {
                "clause_no": no,
                "level": level,
                "category": category,
                "issue": issue[:500],
                "suggestion": str(item.get("suggestion", "")).strip()[:500],
                "confidence": round(confidence, 2),
                "source": "llm",
            }
        )
    return cleaned


def overall_risk(findings: list[dict]) -> str:
    if not findings:
        return "none"
    levels = {f["level"] for f in findings}
    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    return "low"


# ---------------------------------------------------------------- 主流程
def get_active_prompt(db: Session) -> PromptVersion | None:
    stmt = (
        select(PromptVersion)
        .where(PromptVersion.is_active.is_(True))
        .order_by(PromptVersion.version.desc())
    )
    return db.scalars(stmt).first()


def review_contract(
    db: Session,
    contract: Contract,
    *,
    visible_clauses: list[Clause],
    prompt_version: PromptVersion | None = None,
) -> ReviewOutcome:
    """
    对一份合同跑一次审查。

    注意 visible_clauses 是**已经按权限过滤过**的条款 ——
    这一点极其重要：财务审查时，违约责任条款根本不会进入 prompt，
    模型没有机会泄漏它。这比在 prompt 里写"别提违约责任"可靠一万倍。
    """
    pv = prompt_version or get_active_prompt(db)

    clauses_text = render_clauses_text(visible_clauses)
    system_prompt = pv.system_prompt if pv else ""
    user_prompt = (
        pv.user_template.format(
            title=contract.title,
            contract_type=contract.contract_type,
            clauses_text=clauses_text,
        )
        if pv
        else f"合同全文：\n{clauses_text}"
    )

    # ---------- 成本护栏：先估算，超限直接拒，不发请求 ----------
    # 中文大致 1 字 ≈ 1.5 token，这里粗估
    estimated = int((len(system_prompt) + len(user_prompt)) * 1.5)
    if estimated > config.MAX_TOKENS_PER_CONTRACT:
        raise ValueError(
            f"这份合同预计消耗约 {estimated} token，超过单份上限 "
            f"{config.MAX_TOKENS_PER_CONTRACT}。请拆分后提交。"
        )

    result = llm.chat(system_prompt, user_prompt)
    cost = llm.estimate_cost(result.model, result.prompt_tokens, result.completion_tokens)

    # ---------- 模型失败 → 降级到规则引擎 ----------
    if not result.ok:
        rule_findings = rule_based_review(visible_clauses)
        return ReviewOutcome(
            findings=rule_findings,
            summary=(
                f"模型服务当前不可用（{result.error}），已自动切换到规则引擎审查。"
                f"规则引擎识别出 {len(rule_findings)} 处问题，"
                f"准确度低于 AI 审查，建议人工复核全部条款。"
            ),
            risk_level=overall_risk(rule_findings),
            degraded=True,
            degrade_reason=result.error,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=result.latency_ms,
            cost_cny=0.0,
            model_name=f"{result.model}(降级:规则引擎)",
            prompt_version_id=pv.id if pv else None,
        )

    # ---------- 解析 JSON ----------
    data = llm.extract_json(result.text)
    if data is None or not isinstance(data.get("findings"), list):
        # 模型答非所问 → 也降级
        rule_findings = rule_based_review(visible_clauses)
        return ReviewOutcome(
            findings=rule_findings,
            summary=(
                "模型返回的内容无法解析为结构化结果，已切换到规则引擎审查。"
                f"规则引擎识别出 {len(rule_findings)} 处问题，建议人工复核。"
            ),
            risk_level=overall_risk(rule_findings),
            degraded=True,
            degrade_reason="模型输出不是合法 JSON",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            latency_ms=result.latency_ms,
            cost_cny=cost,
            model_name=result.model,
            prompt_version_id=pv.id if pv else None,
        )

    findings = normalize_findings(data.get("findings", []), visible_clauses)
    summary = str(data.get("summary", "")).strip()

    if not summary:
        high = sum(1 for f in findings if f["level"] == "high")
        mid = sum(1 for f in findings if f["level"] == "medium")
        low = sum(1 for f in findings if f["level"] == "low")
        summary = (
            f"共审查 {len(visible_clauses)} 条条款，识别出 {len(findings)} 处风险"
            f"（高 {high} / 中 {mid} / 低 {low}）。"
        )

    return ReviewOutcome(
        findings=findings,
        summary=summary,
        risk_level=overall_risk(findings),
        degraded=False,
        degrade_reason="",
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        latency_ms=result.latency_ms,
        cost_cny=cost,
        model_name=result.model,
        prompt_version_id=pv.id if pv else None,
    )


def save_review(db: Session, contract: Contract, outcome: ReviewOutcome) -> Review:
    """把审查结果落库。"""
    review = Review(
        contract_id=contract.id,
        prompt_version_id=outcome.prompt_version_id,
        model_name=outcome.model_name,
        findings=outcome.findings,
        risk_level=outcome.risk_level,
        summary=outcome.summary,
        degraded=outcome.degraded,
        degrade_reason=outcome.degrade_reason,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        total_tokens=outcome.total_tokens,
        latency_ms=outcome.latency_ms,
        cost_cny=outcome.cost_cny,
    )
    db.add(review)
    contract.status = "reviewed"
    db.commit()
    db.refresh(review)
    return review
