"""
app/services/extract/llm.py — Ollama 客户端兼容层（已通用化）

历史：本项目最初只接本地 Ollama，逻辑写在 extract/llm.py。
现在模型调用已下沉到 app/services/llm/client.py（支持本地 Ollama + 外部 OpenAI 兼容）。
本文件保留为"薄适配层"：
  - 重新导出 LlmError / parse_json_lenient，避免改动既有 import
  - chat_json() 改为按 role 解析当前 ModelConfig 并委托 client 调用
  - health_check() 改为检测默认的模型连接（连通性 + 模型清单）

调用方仍可直接 from app.services.extract.llm import chat_json / LlmError。
"""
from __future__ import annotations

from app.services.llm.client import (
    LlmError,
    chat_json as _client_chat,
    get_default_config,
    get_model_for_role,
    parse_json_lenient,
)

__all__ = ["LlmError", "chat_json", "parse_json_lenient", "health_check"]


def chat_json(
    prompt: str,
    system: str | None = None,
    role: str = "extract",
    model: str | None = None,
    timeout: int | None = None,
    temperature: float | None = None,
) -> dict | list:
    """按 role（默认 extract）解析当前启用的模型配置并调用，返回解析好的 JSON。

    model / timeout / temperature 若显式传入则覆盖配置；否则用配置里的默认值。
    """
    cfg = get_model_for_role(role)
    return _client_chat(cfg, prompt, system=system, model=model, timeout=timeout, temperature=temperature)


def health_check() -> dict:
    """探测默认模型连接的可用性与目标模型是否就绪（供系统健康页展示）。"""
    cfg = get_default_config()
    if cfg is None:
        return {"available": False, "error": "未配置任何模型连接（请到「模型配置」添加）"}

    if cfg.provider == "ollama":
        from app.services.llm.client import list_remote_models

        try:
            names = list_remote_models(cfg)
        except LlmError as e:
            return {"available": False, "provider": cfg.provider, "base_url": cfg.base_url, "error": str(e)}
        target = cfg.model
        ready = any(n == target or n.split(":")[0] == target.split(":")[0] for n in names)
        return {
            "available": True,
            "provider": cfg.provider,
            "base_url": cfg.base_url,
            "model": target,
            "model_ready": ready,
            "installed": names[:20],
        }

    # openai 兼容：连通性已知，模型清单在 test 时才拉，这里只报基本可用
    return {
        "available": True,
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "model_ready": bool(cfg.model),
        "installed": [cfg.model] if cfg.model else [],
    }
