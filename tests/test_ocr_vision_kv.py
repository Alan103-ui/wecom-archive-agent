"""
tests/test_ocr_vision_kv.py — OCR 视觉升级开关「KV 优先」单元测试

验证：
  1. KV 已存值应覆盖 config 默认（前端开关即时生效，无需重启）
  2. KV 无值时回退 config 默认
  3. /health 的 ocr_vision.enabled 反映 KV 当前值
"""
from app.services.ocr import engine as ocr_engine


def _kv(overrides: dict):
    """构造一个 get_setting 桩：命中 overrides 返回值，否则返回 default。"""
    return lambda k, d=None: overrides.get(k, d)


def test_policy_kv_overrides_config(monkeypatch):
    """KV 存了 enabled=True / 阈值=0.3，应覆盖默认 False / 0.6。"""
    monkeypatch.setattr(
        "app.services.kv_store.get_setting",
        _kv({"OCR_VISION_ENABLED": True, "OCR_VISION_MIN_CONFIDENCE": 0.3}),
    )
    enabled, threshold = ocr_engine._ocr_vision_policy()
    assert enabled is True
    assert threshold == 0.3


def test_policy_falls_back_to_config_when_no_kv(monkeypatch):
    """KV 未存任何值时，应回退到 config 默认（False / 0.6）。"""
    monkeypatch.setattr("app.services.kv_store.get_setting", _kv({}))
    enabled, threshold = ocr_engine._ocr_vision_policy()
    assert enabled is False
    assert threshold == 0.6


def test_engine_status_reflects_kv_enabled(monkeypatch):
    """/health 的 ocr_vision.enabled 应反映 KV 当前开关。"""
    monkeypatch.setattr(
        "app.services.kv_store.get_setting",
        _kv({"OCR_VISION_ENABLED": True}),
    )
    monkeypatch.setattr(ocr_engine, "_get_vision_model", lambda: None)
    st = ocr_engine.engine_status()
    assert st["ocr_vision"]["enabled"] is True
    assert st["ocr_vision"]["model_configured"] is False
