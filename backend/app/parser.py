# ==========================================================
#  合同解析
# ----------------------------------------------------------
#  这是整个项目里最容易被低估的工作量。
#
#  为什么重要：审查意见要"定位到第几条"，如果解析出来的条款是错位的，
#  后面 LLM 说什么都是错的。所以这里必须做"解析质量自检"——
#  宁可拒单，也不要输出错位的结果。
# ==========================================================
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 条款切分
# 中文合同的条款标题有很强的规律性，用正则就够，不必上模型
CLAUSE_PATTERNS = [
    # 第一条 / 第六条 违约责任
    re.compile(r"^\s*(第[一二三四五六七八九十百零〇\d]+条)\s*[:：、.]?\s*(.*)$"),
    # 一、二、 / （一）（二）
    re.compile(r"^\s*([一二三四五六七八九十]+)[、.．]\s*(.*)$"),
    re.compile(r"^\s*[（(]([一二三四五六七八九十]+)[)）]\s*(.*)$"),
    # 1. 1.1 1.1.1
    re.compile(r"^\s*(\d+(?:\.\d+){0,3})[、.．]?\s+(.*)$"),
]

# 条款分类关键词表 —— 这是"规则引擎"的核心，
# 模型挂了以后就靠它兜底
CATEGORY_KEYWORDS = {
    "payment": [
        "付款", "支付", "价款", "货款", "账期", "结算", "发票", "预付", "尾款",
        "首付款", "定金", "违约金金额", "费用", "报酬", "对价",
    ],
    "liability": [
        "违约", "赔偿", "责任", "损失", "连带", "损害", "罚款", "处罚", "担责",
    ],
    "termination": [
        "解除", "终止", "到期", "续约", "退出", "退订", "解约", "中止",
    ],
    "confidentiality": [
        "保密", "商业秘密", "机密", "不得披露", "保密义务", "秘密信息",
    ],
    "dispute": [
        "争议", "仲裁", "诉讼", "管辖", "法院", "适用法律", "协商解决",
    ],
    "delivery": [
        "交付", "交货", "验收", "工期", "期限", "完成时间", "进度",
    ],
    "ip": [
        "知识产权", "著作权", "专利", "商标", "成果归属", "所有权",
    ],
}

# 敏感条款的默认可见角色
# 财务只该看付款，不该看违约责任和解除条件（职责分离）
CATEGORY_VISIBILITY = {
    "payment": "all",
    "liability": "submitter,reviewer,admin",
    "termination": "submitter,reviewer,admin",
    "confidentiality": "submitter,reviewer,admin",
    "dispute": "submitter,reviewer,admin",
    "delivery": "all",
    "ip": "submitter,reviewer,admin",
    "other": "all",
}


@dataclass
class ParsedClause:
    order_no: int
    heading: str
    content: str
    category: str = "other"
    visibility: str = "all"


@dataclass
class ParseResult:
    ok: bool
    text: str = ""
    clauses: list[ParsedClause] = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------- 文本抽取
def extract_text(path: Path) -> tuple[str, str]:
    """
    返回 (文本, 错误信息)。支持 .txt/.md/.docx/.pdf。
    """
    suffix = path.suffix.lower()

    if suffix in (".txt", ".md"):
        for enc in ("utf-8", "gbk", "gb18030", "utf-16"):
            try:
                return path.read_text(encoding=enc), ""
            except (UnicodeDecodeError, UnicodeError):
                continue
        return "", "文本编码无法识别，请另存为 UTF-8 后重试"

    if suffix == ".docx":
        try:
            import docx
        except ImportError:
            return "", "服务器缺少 python-docx 依赖"
        try:
            doc = docx.Document(str(path))
            parts: list[str] = []
            for para in doc.paragraphs:
                if para.text.strip():
                    parts.append(para.text)
            # 表格里的内容也是合同的重要部分（付款表、价格表）
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
            return "\n".join(parts), ""
        except Exception as exc:  # noqa: BLE001
            return "", f"DOCX 解析失败：{exc}"

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "服务器缺少 pypdf 依赖"
        try:
            reader = PdfReader(str(path))
            parts = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(text)
            result = "\n".join(parts)
            if len(result.strip()) < 30:
                return "", "PDF 几乎没有可提取的文字（可能是扫描件），需要 OCR 后才能审查"
            return result, ""
        except Exception as exc:  # noqa: BLE001
            return "", f"PDF 解析失败：{exc}"

    return "", f"不支持的文件类型：{suffix}"


# ---------------------------------------------------------------- 分类
def classify_clause(heading: str, content: str) -> str:
    """按关键词给条款分类。命中最多关键词的类别胜出。"""
    blob = f"{heading} {content}"
    scores: dict[str, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in blob)
        if score:
            scores[category] = score
    if not scores:
        return "other"
    return max(scores.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------- 切分
def split_clauses(text: str) -> list[ParsedClause]:
    """
    按条款标题切分正文。
    找不到条款标题时，退化为按段落切分（每段一块），
    保证至少不会整份合同当一个块。
    """
    lines = text.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []

    def flush():
        if current_lines and any(ln.strip() for ln in current_lines):
            blocks.append((current_heading, current_lines[:]))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            current_lines.append("")
            continue

        matched_heading = ""
        for pattern in CLAUSE_PATTERNS:
            m = pattern.match(stripped)
            if m:
                # 第X条 + 标题；或 一、 + 标题
                groups = m.groups()
                if len(groups) >= 2 and groups[1] and len(groups[1]) <= 30:
                    matched_heading = stripped
                elif len(groups) >= 1 and len(stripped) <= 30:
                    matched_heading = stripped
                break

        if matched_heading:
            flush()
            current_heading = matched_heading
            current_lines = []
        else:
            current_lines.append(stripped)
    flush()

    # 一个条款标题都没识别出来 → 按段落切
    if not blocks:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        blocks = [(f"第{i + 1}段", [p]) for i, p in enumerate(paras)]

    clauses: list[ParsedClause] = []
    for idx, (heading, body_lines) in enumerate(blocks, start=1):
        content = "\n".join(body_lines).strip()
        if not content and not heading:
            continue
        category = classify_clause(heading, content)
        clauses.append(
            ParsedClause(
                order_no=idx,
                heading=heading[:250],
                content=content,
                category=category,
                visibility=CATEGORY_VISIBILITY.get(category, "all"),
            )
        )
    return clauses


# ---------------------------------------------------------------- 质量自检（供前端实时验证调用）
def check_quality(text: str, clauses: list[ParsedClause]) -> dict:
    """
    解析质量自检 —— 这是"宁可拒单"的依据。

    检查三件事：
    1. 文本量是否太少（可能是空文件或扫描件）
    2. 条款标题是否命中（命中太少说明结构没解析出来）
    3. 是否存在明显乱码（替换字符比例过高）
    """
    char_count = len(text)
    heading_hits = sum(1 for c in clauses if re.match(r"^第[一二三四五六七八九十百零〇\d]+条", c.heading))
    replacement_chars = text.count("\ufffd") + text.count("锟")

    issues: list[str] = []
    if char_count < 200:
        issues.append(f"正文只有 {char_count} 个字符，太短，可能解析失败或文件为空")
    if heading_hits == 0 and len(clauses) > 0:
        issues.append("未识别到任何「第X条」形式的条款标题，条款结构可能错位")
    if char_count > 0 and replacement_chars / max(char_count, 1) > 0.01:
        issues.append(f"正文含 {replacement_chars} 个乱码字符，占比过高（可能是编码问题）")

    return {
        "char_count": char_count,
        "clause_count": len(clauses),
        "heading_hits": heading_hits,
        "replacement_chars": replacement_chars,
        "issues": issues,
        "ok": len(issues) == 0,
    }


def parse_contract(path: Path, max_chars: int = 60000) -> ParseResult:
    """完整解析流程：抽文本 → 切条款 → 质量自检。"""
    text, err = extract_text(path)
    if err:
        return ParseResult(ok=False, error=err)

    if len(text) > max_chars:
        return ParseResult(
            ok=False,
            error=f"合同正文 {len(text)} 字符，超过单份上限 {max_chars} 字符。请拆分后提交。",
        )

    clauses = split_clauses(text)
    quality = check_quality(text, clauses)

    return ParseResult(
        ok=quality["ok"],
        text=text,
        clauses=clauses,
        quality=quality,
        error="；".join(quality["issues"]),
    )


# ---------------------------------------------------------------- 供前端实时验证调用的解析端点
def parse_text_for_validation(text: str) -> ParseResult:
    """
    专供前端实时验证调用：只解析文本，不涉及文件 I/O。
    用于前端实时预览解析结果、字符计数、条款分段等。
    """
    if len(text) > 60000:
        return ParseResult(
            ok=False,
            error=f"合同正文 {len(text)} 字符，超过单份上限 60000 字符。请拆分后提交。",
        )

    clauses = split_clauses(text)
    quality = check_quality(text, clauses)

    return ParseResult(
        ok=quality["ok"],
        text=text,
        clauses=clauses,
        quality=quality,
        error="；".join(quality["issues"]),
    )


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
