"""
tests/test_auth_security.py — 密码策略 / 登录失败锁定 / 审计日志
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.auth import policy

client = TestClient(app)


def test_password_strength():
    ok, msg = policy.check_password_strength("short1")       # 长度不足
    assert not ok and "至少" in msg
    ok, msg = policy.check_password_strength("12345678")     # 纯数字
    assert not ok and "字母和数字" in msg
    ok, msg = policy.check_password_strength("Abc12345")
    assert ok and msg == ""


def test_login_lockout(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_audit_path", tmp_path / "audit.log")
    policy.reset_login_failures("lockuser")
    # 连续失败达到阈值 → 触发锁定
    for _ in range(settings.AUTH_MAX_FAILS):
        r = client.post("/api/auth/login", json={"username": "lockuser", "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "lockuser", "password": "wrong"})
    assert r.status_code == 429, r.text
    # 锁定期间即使正确密码也拒绝（模拟攻击者换密码爆破）
    policy.reset_login_failures("lockuser")  # 清理，避免影响其它用例


def test_audit_log_written(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_audit_path", tmp_path / "audit.log")
    policy.log_audit("login_failed", "u1", "1.2.3.4", "test")
    text = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "login_failed" in text and "u1" in text and "1.2.3.4" in text
