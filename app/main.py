"""
app/main.py — 应用入口

启动顺序：
    建目录 → 建表 → 播种默认模板 → 启动调度器 → 挂载路由与前端

运行：
    python -m app.main
    或 uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.config import BASE_DIR, settings
from app.db.database import SessionLocal, init_db
from app.scheduler import shutdown_scheduler, start_scheduler
from app.services.extract.templates import seed_templates
from app.services.llm.seed import seed_model_defaults
from app.services.risk.seed import seed_risk_defaults
from app.services.rooms_seed import seed_default_rooms
from app.services.auth.catalog import seed_auth
from app.services.license.manager import get_license_status


class NoCacheStaticFiles(StaticFiles):
    """静态文件禁用浏览器缓存，避免前端改了用户还加载旧 JS。"""
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.update({"Cache-Control": "no-store, must-revalidate"})
        return response

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# 第三方库日志降噪
for noisy in ("httpx", "apscheduler.executors.default", "PIL"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("app")

FRONTEND_DIR = BASE_DIR / "frontend"


def _check_license() -> None:
    """启动时校验 License：非强制模式仅提示；强制模式无效时告警（不阻断启动，便于排障）。"""
    st = get_license_status()
    if st["status"] == "valid":
        if st.get("days_left") is not None and st["days_left"] <= settings.LICENSE_GRACE_DAYS:
            logger.warning("License 将于 %s 到期（剩 %s 天），请尽快续费。", st.get("expire_at"), st.get("days_left"))
        else:
            logger.info("License 有效：客户=%s，到期=%s", st.get("customer"), st.get("expire_at"))
    elif st["status"] == "grace":
        logger.warning("License 已到期，处于 %s 天宽限期，请尽快续费。", settings.LICENSE_GRACE_DAYS)
    elif st["status"] == "not_found":
        if settings.LICENSE_REQUIRED:
            logger.warning("未找到 License 且 LICENSE_REQUIRED=true：系统将以受限模式运行，请尽快激活。")
        else:
            logger.info("未配置 License（开发/演示模式，不强制校验）。")
    else:
        logger.warning("License 状态异常（%s）：%s", st.get("status"), st.get("message"))


def _prepare_dirs() -> None:
    for p in (
        Path(settings.MEDIA_ROOT),
        BASE_DIR / "data",
        BASE_DIR / "data" / "sdk",
        BASE_DIR / "data" / "fixtures",
    ):
        p.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _prepare_dirs()
    _check_license()
    init_db()
    logger.info("数据库就绪：%s", settings.DATABASE_URL.split("://")[0])

    db = SessionLocal()
    try:
        seed_templates(db)
        seed_model_defaults(db)
        seed_risk_defaults(db)
        seed_default_rooms(db)
        seed_auth(db)
    finally:
        db.close()

    start_scheduler()
    logger.info(
        "%s 已启动 → http://127.0.0.1:%d  （采集模式：%s）",
        settings.APP_NAME, settings.PORT, settings.COLLECTOR_MODE,
    )
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "企业微信会话内容存档 → OCR → 大模型结构化 → 业务基础数据表。\n\n"
        "接口文档：/docs　管理页：/"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # 仅同源（SPA 同源部署）默认即可；跨域需显式白名单，避免 "*" + credentials 的越权风险
    allow_origins=[o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/WW_verify_{token}.txt", include_in_schema=False)
def wecom_verify_file(token: str):
    """企微可信域名验证：根目录校验文件。

    将 WW_verify_*.txt 放在 frontend/ 下即可被企微访问到
    http://<域名>/WW_verify_*.txt，用于完成可信域名归属认证。
    """
    f = FRONTEND_DIR / f"WW_verify_{token}.txt"
    if f.exists() and f.is_file():
        return FileResponse(
            f,
            media_type="text/plain",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
    return JSONResponse(status_code=404, content={"detail": "not found"})


# 前端：后端直接托管静态文件，不另起端口
if FRONTEND_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        f = FRONTEND_DIR / "index.html"
        if f.exists():
            return FileResponse(f, headers={"Cache-Control": "no-store, must-revalidate"})
        return JSONResponse({"message": settings.APP_NAME, "docs": "/docs"})
else:  # pragma: no cover

    @app.get("/", include_in_schema=False)
    def index():
        return JSONResponse({"message": settings.APP_NAME, "docs": "/docs"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,  # 调度器在 reload 下会双起，需要热重载时用命令行显式开
    )
