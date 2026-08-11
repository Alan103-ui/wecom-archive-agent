"""
app/db/database.py — 数据库引擎与会话

SQLite 与 PostgreSQL 双兼容：
- SQLite 需要 check_same_thread=False（调度线程与请求线程共用引擎）
- SQLite 开启 WAL，避免"采集线程写 / 接口线程读"互相阻塞
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _build_engine():
    if settings.is_sqlite:
        eng = create_engine(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
            echo=False,
        )

        # WAL 模式：读写并发不打架；同步级别降到 NORMAL 提升写入吞吐
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng

    # PostgreSQL / 其他
    return create_engine(
        settings.DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=False,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_sqlite() -> None:
    """SQLite 不支持 ALTER ADD COLUMN via create_all，这里补齐新增列，避免旧库崩。
    仅项目开发迭代用；生产环境建议用 Alembic。"""
    import sqlite3

    db_path = settings.DATABASE_URL.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(chat_message)")
        cols = {r[1] for r in cur.fetchall()}
        if "risk_scanned" not in cols:
            cur.execute("ALTER TABLE chat_message ADD COLUMN risk_scanned INTEGER NOT NULL DEFAULT 0")
            conn.commit()

        # wecom_config 新增列（客户联系 secret）
        cur.execute("PRAGMA table_info(wecom_config)")
        wcols = {r[1] for r in cur.fetchall()}
        if "customer_contact_secret" not in wcols:
            cur.execute("ALTER TABLE wecom_config ADD COLUMN customer_contact_secret TEXT NOT NULL DEFAULT ''")
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """建表。后台线程与主进程都可安全调用（CREATE TABLE IF NOT EXISTS 语义）"""
    from app.models import entities  # noqa: F401  确保所有模型已注册到 Base.metadata
    from app.models import risk  # noqa: F401  注册风险与预警模型
    from app.models import model_config  # noqa: F401  注册模型连接配置

    from pathlib import Path

    if settings.is_sqlite:
        # sqlite:///D:/path/data/archive.db  → 确保父目录存在
        db_path = settings.DATABASE_URL.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)

    if settings.is_sqlite:
        _migrate_sqlite()
