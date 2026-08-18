"""
tests/test_auth_rbac.py — 登录认证 + RBAC 权限（模块/按钮级）集成测试

覆盖：
  1. 登录成功/失败/错误密码
  2. 未登录访问业务接口 → 401
  3. 超管绕过权限
  4. 只读用户：可读不可写（按钮权限拦截 403）
  5. 用户管理 CRUD 与保护（超管不可删）
  6. 角色管理 CRUD 与内置角色保护
  7. 修改密码
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _client():
    return TestClient(app)


def _login(c: TestClient, username: str, password: str) -> dict:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_login_ok_and_unauthorized_gate():
    with _client() as c:
        # 错误密码
        r = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
        # 正确密码
        r = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        data = r.json()
        assert data["user"]["is_super"] is True
        # 未登录访问业务接口 → 401
        assert c.get("/api/rooms").status_code == 401
        assert c.get("/api/records?page_size=1").status_code == 401
        # 登录后 → 200
        h = {"Authorization": "Bearer " + data["token"]}
        assert c.get("/api/rooms", headers=h).status_code == 200


def test_super_admin_bypass_and_crud_users():
    with _client() as c:
        h = _login(c, "admin", "admin123")
        # 角色列表含三个内置角色
        roles = c.get("/api/roles", headers=h).json()
        codes = {r["code"] for r in roles}
        assert {"admin", "operator", "viewer"} <= codes
        viewer = next(r for r in roles if r["code"] == "viewer")
        # 新建用户
        r = c.post("/api/users", headers=h, json={
            "username": "unit_reader", "password": "pass123456",
            "display_name": "单测只读", "role_ids": [viewer["id"]],
        })
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        # 修改用户
        assert c.patch(f"/api/users/{uid}", headers=h, json={"display_name": "改个名"}).status_code == 200
        # 超管不可删除
        admin = next(u for u in c.get("/api/users", headers=h).json() if u["is_super"])
        assert c.delete(f"/api/users/{admin['id']}", headers=h).status_code == 400
        # 删除单测用户
        assert c.delete(f"/api/users/{uid}", headers=h).status_code == 200


def test_viewer_read_only_enforced():
    with _client() as c:
        admin_h = _login(c, "admin", "admin123")
        viewer = next(r for r in c.get("/api/roles", headers=admin_h).json() if r["code"] == "viewer")
        c.post("/api/users", headers=admin_h, json={
            "username": "unit_reader2", "password": "pass123456", "role_ids": [viewer["id"]],
        })
        vh = _login(c, "unit_reader2", "pass123456")
        # 读 OK
        assert c.get("/api/records?page_size=1", headers=vh).status_code == 200
        assert c.get("/api/templates", headers=vh).status_code == 200
        # 写被拦截（按钮级权限）
        r = c.delete("/api/records/not-exist", headers=vh)
        assert r.status_code == 403
        assert "records:delete" in r.json()["detail"]
        r = c.post("/api/templates", headers=vh, json={
            "name": "should-fail", "fields_schema": [{"key": "a", "label": "A", "type": "string"}],
        })
        assert r.status_code == 403
        # 用户/角色管理写操作也拦截
        assert c.post("/api/users", headers=vh, json={
            "username": "x", "password": "pass123456",
        }).status_code == 403
        assert c.post("/api/roles", headers=vh, json={
            "name": "x", "code": "xx",
        }).status_code == 403
        # 清理
        uid = next(u for u in c.get("/api/users", headers=admin_h).json() if u["username"] == "unit_reader2")["id"]
        c.delete(f"/api/users/{uid}", headers=admin_h)


def test_role_crud_and_builtin_protection():
    with _client() as c:
        h = _login(c, "admin", "admin123")
        # 新建角色
        r = c.post("/api/roles", headers=h, json={"name": "单测角色", "code": "unit_role", "description": "t"})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        # 内置角色不可删除
        builtin = next(rr for rr in c.get("/api/roles", headers=h).json() if rr["is_builtin"])
        assert c.delete(f"/api/roles/{builtin['id']}", headers=h).status_code == 400
        # 自定义角色可删
        assert c.delete(f"/api/roles/{rid}", headers=h).status_code == 200


def test_change_password():
    with _client() as c:
        admin_h = _login(c, "admin", "admin123")
        # 错误原密码
        r = c.post("/api/auth/change-password", headers=admin_h,
                   json={"old_password": "bad", "new_password": "newpass123"})
        assert r.status_code == 400
        # 正确原密码
        r = c.post("/api/auth/change-password", headers=admin_h,
                   json={"old_password": "admin123", "new_password": "admin123"})
        assert r.status_code == 400  # 新旧相同被拒
        # 恢复原样（不真的改，避免污染开发库）——上面已被拒，密码仍是 admin123
