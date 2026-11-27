# ==========================================================
#  认证与授权
# ----------------------------------------------------------
#  三个层次，缺一不可：
#  1. 认证（Authentication）：你是谁 —— JWT 令牌
#  2. 授权（Authorization）：你能做什么 —— 角色检查
#  3. 数据隔离（Data Isolation）：你能看到哪些行 —— SQL 层过滤
#
#  初学者最容易只做第 1 层，然后在界面上把按钮藏起来当权限。
#  那不是权限，那是装饰。
# ==========================================================
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import config
from .database import get_db
from .models import AuditLog, User

bearer_scheme = HTTPBearer(auto_error=False)

# 角色 → 中文名，前端展示用
ROLE_NAMES = {
    "submitter": "业务提交人",
    "reviewer": "法务审查员",
    "finance": "财务",
    "admin": "管理员",
}


# ---------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    """
    用 bcrypt 加盐哈希。
    为什么不用 SHA256？因为 SHA256 太快，GPU 一秒钟能试几十亿次。
    bcrypt 故意慢（默认 12 轮），暴力破解成本高几个数量级。
    bcrypt 会自动把盐存在哈希串里，不用自己管理。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- JWT
def create_access_token(user: User) -> str:
    """
    JWT 里放 user_id 和 role。
    注意：不要把密码、手机号这些敏感信息放进 payload —— JWT 只是签名，
    不是加密，任何人都能 base64 解开看内容。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=config.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证无效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------- 依赖
def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """从 Authorization: Bearer <token> 解出当前用户。"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    user_id = int(payload.get("sub", 0))
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: str):
    """
    角色门禁。用法：user: Annotated[User, Depends(require_roles("reviewer", "admin"))]

    这是"能做什么"的检查；"能看哪些行"还要在查询里再过滤一次。
    """

    def _checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"当前角色（{ROLE_NAMES.get(user.role, user.role)}）无权执行此操作",
            )
        return user

    return _checker


# ---------------------------------------------------------------- 数据可见范围
def contract_visibility_filter(user: User):
    """
    返回一个 SQLAlchemy 过滤条件，直接用在 WHERE 里。

    - 法务审查员：能看全部（审查是跨部门的职能）
    - 管理员：一行也不返回。管理员负责统计、审计和配置，职责分离，
      系统管理员不该有权限读业务内容（README / 学习手册里承诺的就是这条）
    - 财务：只能看付款条款相关的合同？不对 —— 财务需要看到合同本身，
      但只能看到 payment 类条款（那是条款级的过滤，见 clause_visible）
    - 业务提交人：只能看自己部门的

    重点：这是 SQL 条件，不是"查出来再过滤"。
    数据库根本不会返回越权行，内存里也就不存在泄漏的可能。
    """
    from sqlalchemy import false

    from .models import Contract

    if user.role == "admin":
        return false()
    if user.role == "reviewer":
        return True  # 用 sqlalchemy.true() 也行，这里简化
    if user.role == "finance":
        # 财务能看所有部门的合同（要审付款），但条款级会再过滤
        return True
    # submitter
    if user.dept_id is None:
        return Contract.submitter_id == user.id
    from sqlalchemy import or_

    return or_(Contract.dept_id == user.dept_id, Contract.submitter_id == user.id)


def clause_visible(clause_visibility: str, user: User) -> bool:
    """
    条款级权限。

    设计原则：宁可少给，不可多给。
    默认 "all" 表示所有人可见；否则按逗号分隔的角色列表匹配。
    财务看不到 liability / termination 类条款 —— 这在 visibility 字段里写死，
    而不是靠 prompt 说"别提"。

    管理员例外：即使 visibility 里写了 admin（历史数据里就是这么存的），
    也一律不给。职责分离要有代码兜底，不能只靠数据里少写一个词。
    """
    if user.role == "admin":
        return False
    if clause_visibility in ("all", "", None):
        return True
    allowed = {r.strip() for r in clause_visibility.split(",") if r.strip()}
    return user.role in allowed


# ---------------------------------------------------------------- 审计
def write_audit(
    db: Session,
    *,
    user: User | None,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    detail: dict | None = None,
    request: Request | None = None,
    success: bool = True,
) -> None:
    """
    写一条审计日志。
    注意：这里只 add + commit，没有提供任何 update/delete 的入口 ——
    append only 是审计可信的前提。
    """
    ip = ""
    ua = ""
    if request is not None:
        # 反向代理场景要读 X-Forwarded-For，这里本机部署，直连即可
        ip = request.client.host if request.client else ""
        ua = (request.headers.get("user-agent") or "")[:255]

    db.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "",
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
            ip_address=ip,
            user_agent=ua,
            success=success,
        )
    )
    db.commit()
