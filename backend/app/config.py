# ==========================================================
#  企业合同智能审查系统 —— 后端配置
# ----------------------------------------------------------
#  安全原则：所有敏感值只从 .env 读取，绝不写死在源码里。
#  换模型 / 换服务商，只需要改 .env 里的三行。
# ==========================================================
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # ---------- 基础 ----------
    APP_NAME = "企业合同智能审查系统"
    APP_VERSION = "1.0.0"
    DEBUG = os.getenv("DEBUG", "true").lower() == "true"

    # ---------- 服务 ----------
    # 注意：用 127.0.0.1 而不是 localhost。
    # Windows 上 localhost 会同时解析到 ::1 和 127.0.0.1，
    # 浏览器优先走 IPv6，而 uvicorn 默认只监听 IPv4，会导致请求挂 30 秒。
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8210"))

    # ---------- 数据库 ----------
    DB_PATH = BASE_DIR / os.getenv("DB_FILE", "contract.db")
    DB_URL = f"sqlite:///{DB_PATH}"

    # ---------- 认证 ----------
    # JWT 密钥：首次启动时若 .env 没有，会自动生成并写回。
    JWT_SECRET = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", str(60 * 8)))  # 8 小时

    # ---------- LLM（智谱 OpenAI 兼容端点）----------
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "glm-4-flashx")

    # 审查是"要准"不是"要活"，温度必须低
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
    LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", "10"))
    LLM_READ_TIMEOUT = float(os.getenv("LLM_READ_TIMEOUT", "120"))

    # ---------- 成本护栏 ----------
    # 单份合同允许消耗的最大 token（超出直接拒单，不发请求）
    MAX_TOKENS_PER_CONTRACT = int(os.getenv("MAX_TOKENS_PER_CONTRACT", "30000"))
    # 每个用户每天允许审查的合同份数
    MAX_CONTRACTS_PER_USER_PER_DAY = int(os.getenv("MAX_CONTRACTS_PER_USER_PER_DAY", "50"))
    # 单份合同字符数上限（防止超大文件把上下文打爆）
    MAX_CONTRACT_CHARS = int(os.getenv("MAX_CONTRACT_CHARS", "60000"))

    # ---------- 上传 ----------
    UPLOAD_DIR = BASE_DIR / "uploads"
    ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))

    # ---------- 审计 ----------
    # 审计日志只增不改，这是合规的底线
    AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))


config = Config()

# 首次启动自动生成 JWT 密钥并持久化，避免重启后所有人被登出
if not config.JWT_SECRET:
    import secrets

    _env_path = BASE_DIR / ".env"
    _secret = secrets.token_urlsafe(48)
    with open(_env_path, "a", encoding="utf-8") as _f:
        _f.write(f"\nJWT_SECRET={_secret}\n")
    config.JWT_SECRET = _secret
    print("[config] 已生成新的 JWT_SECRET 并写入 .env")

config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
