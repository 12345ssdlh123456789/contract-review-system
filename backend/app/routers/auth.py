# ==========================================================
#  认证路由：登录 / 当前用户 / 登出
# ----------------------------------------------------------
#  演示系统不开放注册 —— 企业系统的账号由管理员统一开通，
#  这里提供 4 个内置演示账号，密码统一在 README 里说明。
# ==========================================================
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..models import Department, User
from ..schemas import LoginRequest, TokenResponse, UserOut
from ..security import (
    ROLE_NAMES,
    CurrentUser,
    create_access_token,
    hash_password,
    verify_password,
    write_audit,
)

router = APIRouter(prefix="/api/auth", tags=["认证"])

# 用户不存在时拿来做一次等价耗时的密码校验，避免通过响应时间枚举账号。
# 启动时算一次，不放在请求路径里。
_DUMMY_HASH = hash_password("timing-attack-placeholder")


def to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        role_name=ROLE_NAMES.get(user.role, user.role),
        dept_id=user.dept_id,
        dept_name=user.department.name if user.department else "",
        is_active=user.is_active,
        last_login_at=user.last_login_at,
    )


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """
    登录。

    安全细节（企业级必须做）：
    - 用户不存在 和 密码错误 返回**同一句提示**，避免账号枚举
    - 失败也写审计日志（这是排查暴力破解的唯一依据）
    - 成功时记录最后登录时间
    """
    stmt = select(User).where(User.username == payload.username)
    user = db.scalars(stmt).first()

    if user is None:
        # 账号不存在也要跑一次 bcrypt（≈12 轮），让两条失败路径耗时接近。
        verify_password(payload.password, _DUMMY_HASH)
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.password_hash)

    if user is None or not password_ok:
        write_audit(
            db,
            user=user,
            action="login_failed",
            target_type="user",
            target_id=user.id if user else None,
            detail={"username": payload.username},
            request=request,
            success=False,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
        )

    if not user.is_active:
        write_audit(
            db, user=user, action="login_disabled", target_type="user",
            target_id=user.id, request=request, success=False,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用，请联系管理员")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    write_audit(
        db, user=user, action="login_success", target_type="user",
        target_id=user.id, request=request,
    )

    token = create_access_token(user)
    return TokenResponse(
        access_token=token,
        expires_in=config.JWT_EXPIRE_MINUTES * 60,
        user=to_user_out(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return to_user_out(user)


@router.post("/logout")
def logout(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """
    登出。JWT 是无状态的，服务端无法"作废"已签发的令牌，
    所以这里只写审计日志。真正的失效靠前端删 token + 令牌过期。
    生产环境要配合 Redis 黑名单或短有效期 + refresh token。
    """
    write_audit(
        db, user=user, action="logout", target_type="user",
        target_id=user.id, request=request,
    )
    return {"ok": True, "message": "已登出"}


@router.get("/users", response_model=list[UserOut])
def list_users(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    """列出所有账号（管理员/审查员可见，方便切换演示）。"""
    if user.role not in ("admin", "reviewer"):
        raise HTTPException(status_code=403, detail="仅管理员和法务审查员可查看账号列表")
    users = db.scalars(select(User).order_by(User.id)).all()
    return [to_user_out(u) for u in users]


@router.get("/departments")
def list_departments(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    depts = db.scalars(select(Department).order_by(Department.id)).all()
    return [{"id": d.id, "name": d.name, "code": d.code} for d in depts]
