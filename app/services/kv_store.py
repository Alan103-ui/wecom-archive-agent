"""
app/services/kv_store.py — 通用键值设置的读写 helper

与 app/models/kv.py（模型）分离，集中放"取/存"逻辑，避免到处手写 Session。
典型用途：前端可改、无需重启即可生效的运行期开关（超时提醒、抽取模式等）。
"""
from __future__ import annotations

from app.db.database import SessionLocal
from app.models.kv import KVSetting


def get_setting(key: str, default=None):
    """读取一个设置值；不存在返回 default。"""
    db = SessionLocal()
    try:
        row = db.get(KVSetting, key)
        return row.value_json if row is not None else default
    finally:
        db.close()


def set_setting(key: str, value) -> None:
    """写入（upsert）一个设置值。"""
    db = SessionLocal()
    try:
        row = db.get(KVSetting, key)
        if row is None:
            row = KVSetting(key=key, value_json=value)
            db.add(row)
        else:
            row.value_json = value
        db.commit()
    finally:
        db.close()
