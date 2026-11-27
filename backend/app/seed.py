# ==========================================================
#  初始化数据：部门、演示账号、prompt 版本、评测集
# ----------------------------------------------------------
#  运行：python -m app.seed
#  幂等：重复跑不会产生重复数据
# ==========================================================
from sqlalchemy import select

from .database import SessionLocal, init_db
from .eval_runner import seed_eval_cases
from .models import Department, PromptVersion, User
from .prompts import DEFAULT_PROMPTS
from .security import hash_password

# 演示账号：用户名 / 密码 / 显示名 / 角色 / 部门
DEMO_USERS = [
    ("submitter", "demo123456", "张伟（业务部）", "submitter", "SALES"),
    ("reviewer", "demo123456", "李静（法务部）", "reviewer", "LEGAL"),
    ("finance", "demo123456", "王强（财务部）", "finance", "FINANCE"),
    ("admin", "demo123456", "系统管理员", "admin", None),
]

DEPARTMENTS = [
    ("SALES", "业务部"),
    ("LEGAL", "法务部"),
    ("FINANCE", "财务部"),
    ("TECH", "技术部"),
    ("HR", "人力资源部"),
]


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        # ---------- 部门 ----------
        for code, name in DEPARTMENTS:
            if not db.scalars(select(Department).where(Department.code == code)).first():
                db.add(Department(code=code, name=name))
        db.commit()
        depts = {d.code: d for d in db.scalars(select(Department)).all()}
        print(f"[seed] 部门 {len(depts)} 个")

        # ---------- 账号 ----------
        for username, password, display, role, dept_code in DEMO_USERS:
            if db.scalars(select(User).where(User.username == username)).first():
                print(f"[seed] 账号 {username} 已存在，跳过")
                continue
            db.add(
                User(
                    username=username,
                    display_name=display,
                    password_hash=hash_password(password),
                    role=role,
                    dept_id=depts[dept_code].id if dept_code else None,
                    is_active=True,
                )
            )
            print(f"[seed] 创建账号 {username} / {role}")
        db.commit()

        # ---------- Prompt 版本 ----------
        existing_versions = {
            p.version for p in db.scalars(select(PromptVersion)).all()
        }
        for version, system_prompt, user_template, note, is_active in DEFAULT_PROMPTS:
            if version in existing_versions:
                continue
            db.add(
                PromptVersion(
                    name="contract_review",
                    version=version,
                    system_prompt=system_prompt,
                    user_template=user_template,
                    note=note,
                    is_active=is_active,
                )
            )
            print(f"[seed] 创建 prompt v{version}")
        db.commit()

        # ---------- 评测集 ----------
        added = seed_eval_cases(db)
        total_cases = db.query(__import__("app.models", fromlist=["EvalCase"]).EvalCase).count()
        print(f"[seed] 评测集新增 {added} 条，共 {total_cases} 条")

        print("\n[seed] 完成。演示账号（密码统一 demo123456）：")
        for username, _, display, role, _ in DEMO_USERS:
            print(f"  {username:10s} {display:16s} 角色={role}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
