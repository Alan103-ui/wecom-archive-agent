"""
scripts/migrate_sqlite_to_pg.py — SQLite → PostgreSQL 全量迁移（私有化生产化）

用法：
  1) 目标机准备 PostgreSQL 并建库（UTF-8）：
       CREATE DATABASE wecom_archive ENCODING 'UTF8';
  2) 执行迁移（自动建表 + 全量拷贝，表顺序按外键依赖自动排序）：
       python scripts/migrate_sqlite_to_pg.py \\
           --pg postgresql+psycopg://user:pwd@host:5432/wecom_archive
  3) 把 .env 的 DATABASE_URL 切换为目标 PG 连接串，重启服务即完成切换。

说明：本项目主键均为字符串 UUID，无需处理自增序列。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine

from app.db.database import Base, engine as sqlite_engine


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 全量迁移")
    ap.add_argument("--pg", required=True, help="PG 连接串，如 postgresql+psycopg://user:pwd@host:5432/wecom_archive")
    args = ap.parse_args()

    # 注册全部模型到 Base.metadata（与 init_db 保持一致）
    import app.models.entities  # noqa: F401
    import app.models.auth  # noqa: F401
    import app.models.risk  # noqa: F401
    import app.models.model_config  # noqa: F401
    import app.models.kv  # noqa: F401

    pg_engine = create_engine(args.pg, pool_pre_ping=True)
    print(f"源库：{sqlite_engine.url}")
    print(f"目标：{args.pg.split('@')[-1]}")

    Base.metadata.create_all(bind=pg_engine)
    print("目标库建表完成（create_all）")

    total = 0
    with sqlite_engine.connect() as src:
        with pg_engine.connect() as dst:
            for table in Base.metadata.sorted_tables:
                rows = src.execute(table.select()).mappings().all()
                if not rows:
                    print(f"  {table.name}: 0 行（跳过）")
                    continue
                dst.execute(table.insert(), [dict(r) for r in rows])
                total += len(rows)
                print(f"  {table.name}: {len(rows)} 行")
            dst.commit()
    print(f"迁移完成，共 {total} 行。请切换 .env 的 DATABASE_URL 后重启服务。")


if __name__ == "__main__":
    main()
