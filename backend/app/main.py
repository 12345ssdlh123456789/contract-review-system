# ==========================================================
#  应用入口
# ----------------------------------------------------------
#  启动：uvicorn app.main:app --reload --port 8210
#  文档：http://127.0.0.1:8210/docs
# ==========================================================
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import config
from .database import init_db
from .routers import admin, auth, contracts, eval as eval_router, parser


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print(f"[启动] {config.APP_NAME} v{config.APP_VERSION}")
    print(f"[启动] 数据库：{config.DB_PATH}")
    print(f"[启动] 模型：{config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    if not config.LLM_API_KEY:
        print("[警告] 未配置 LLM_API_KEY，审查会走规则引擎降级")
    yield
    print("[关闭] 服务已停止")


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    description=(
        "企业级合同智能审查系统 —— 演示项目\n\n"
        "**角色**：submitter（业务提交人）/ reviewer（法务审查员）/ finance（财务）/ admin（管理员）\n\n"
        "**演示账号**：submitter / reviewer / finance / admin，密码均为 `demo123456`"
    ),
    lifespan=lifespan,
)

# 允许前端跨域。生产环境要收窄到具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # 用 Bearer token，不需要 cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 请求日志
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    """记录每个请求耗时 —— 生产环境应该接 OpenTelemetry。"""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed:.1f}"
    return response


# ---------------------------------------------------------------- 全局异常
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    兜底异常处理。
    注意：不要把 traceback 返回给前端 —— 那是给攻击者送情报。
    详细信息写服务端日志。
    """
    import traceback

    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请联系管理员查看日志"},
    )


# ---------------------------------------------------------------- 路由
app.include_router(auth.router)
app.include_router(contracts.router)
app.include_router(admin.router)
app.include_router(eval_router.router)
app.include_router(parser.router)


@app.get("/api/health", tags=["系统"])
def health():
    """健康检查。生产环境要检查数据库连接、模型可达性。"""
    return {
        "ok": True,
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "model": config.LLM_MODEL,
        "llm_configured": bool(config.LLM_API_KEY),
    }
