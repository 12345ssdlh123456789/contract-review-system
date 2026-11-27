# ==========================================================
#  解析器验证路由（供前端实时验证）
# ----------------------------------------------------------
#  前端输入文本时实时调用，返回：
#  - 解析出的条款列表
#  - 质量自检结果
#  - 错误信息（如有）
# ==========================================================
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..parser import parse_text_for_validation
from ..schemas import ParserValidateRequest, ParserValidateResponse
from ..security import CurrentUser

router = APIRouter(prefix="/api/parser", tags=["解析器"])


@router.post("/validate", response_model=ParserValidateResponse)
def validate_contract_text(
    payload: ParserValidateRequest,
    user: CurrentUser,
    db: Session = Depends(get_db),
):
    """
    前端实时验证：输入合同文本，返回解析出的条款、质量自检结果。
    
    用于新建合同弹窗中「粘贴文本」Tab 的实时预览。
    """
    if not payload.content or not payload.content.strip():
        return ParserValidateResponse(ok=True, clauses=[], quality={"ok": True, issues: []}, error="")
    
    result = parse_text_for_validation(payload.content)
    
    clauses_info = [
        {
            "order_no": c.order_no,
            "heading": c.heading,
            "content": c.content,
            "category": c.category,
            "visibility": c.visibility,
        }
        for c in result.clauses
    ]
    
    return ParserValidateResponse(
        ok=result.ok,
        clauses=clauses_info,
        quality=result.quality,
        error=result.error,
    )