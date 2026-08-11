"""
app/services/llm/client.py — 通用化模型客户端（本地 Ollama + 外部 OpenAI 兼容）

职责：
1. 把"不同提供方、不同调用协议"统一成同一个 chat_json() 入口。
2. 按 role 解析当前应使用哪个 ModelConfig（带短缓存，避免每次调用都查库）。
3. JSON 健壮解析（模型输出常被代码块/废话/括号错配污染，需要"抽取+修复"）。
4. 提供 list_remote_models()（拉远端模型清单）与 test_model()（连通性+样例调用），
   供前端「模型配置」界面做可视化和连通性自检。

调用方（抽取 / 风险研判）只需：
    from app.services.llm.client import chat_json, get_model_for_role
    cfg = get_model_for_role("extract")
    data = chat_json(cfg, prompt, system=...)
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import httpx

from app.db.database import SessionLocal
from app.models.model_config import (
    PROVIDER_OPENAI,
    PROVIDER_OLLAMA,
    ModelConfig,
)

logger = logging.getLogger(__name__)


class LlmError(RuntimeError):
    pass


def _normalize_base_url(url: str) -> str:
    """统一 Base URL：仅做去空白 / 去尾斜杠。

    注意：不再剥离末尾的 /v1 / /v4 等路径段——因为不同厂商的 OpenAI 兼容
    端点路径各不相同（OpenAI/SiliconFlow=…/v1，智谱=…/api/paas/v4），
    强行剥离会让智谱变成 …/api/paas/v4 → 再硬拼 /v1 而 404。
    真正拼接 chat/completions、models 的归一化交给 _openai_endpoint()。
    """
    return (url or "").strip().rstrip("/")


def _openai_endpoint(base_url: str, suffix: str) -> str:
    """构造 OpenAI 兼容端点地址。

    - 若 base_url 已包含 OpenAI 风格的 API 路径段（/v1 /v2 /v3 /v4 /openai /api/…），
      说明用户已填到了"接口前缀"，直接拼接 /chat/completions 或 /models，不再补 /v1。
      例：https://open.bigmodel.cn/api/paas/v4 → …/api/paas/v4/chat/completions
    - 否则（只填了 host 根，如 https://api.openai.com），自动补 /v1。
      例：https://api.siliconflow.cn → …/v1/chat/completions
    suffix 形如 'chat/completions' 或 'models'。
    """
    base = _normalize_base_url(base_url)
    low = base.lower()
    has_api_path = any(
        seg in low for seg in ("/v1", "/v2", "/v3", "/v4", "/openai", "/api/")
    )
    if has_api_path:
        return f"{base}/{suffix}"
    return f"{base}/v1/{suffix}"


# ---------------------------------------------------------------- JSON 修复
def _strip_wrapper(text: str) -> str:
    """剥掉代码块围栏与前后废话，定位到第一个 { 或 ["""
    t = text.strip()
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    start = min(
        (i for i in (t.find("{"), t.find("[")) if i != -1),
        default=-1,
    )
    if start > 0:
        t = t[start:]
    return t.strip()


def _repair_brackets(text: str) -> str:
    """栈式括号修复：跳过字符串字面量内部的括号，末尾补齐未闭合括号，丢弃多余闭合符。"""
    stack: list[str] = []
    in_str = False
    escape = False
    out: list[str] = []

    for ch in text:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\" and in_str:
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str:
            out.append(ch)
            continue
        if ch in "{[":
            stack.append(ch)
            out.append(ch)
        elif ch in "}]":
            want = "{" if ch == "}" else "["
            if stack and stack[-1] == want:
                stack.pop()
                out.append(ch)
            # 多余的闭合符丢弃
        else:
            out.append(ch)

    result = "".join(out)
    if in_str:
        result += '"'
    result = re.sub(r",\s*$", "", result.rstrip())
    while stack:
        result += "}" if stack.pop() == "{" else "]"
    return result


def _normalize_quotes(text: str) -> str:
    return text.replace("“", '"').replace("”", '"')


def parse_json_lenient(raw: str) -> dict | list:
    """尽最大努力把模型输出解析成 JSON"""
    if not raw or not raw.strip():
        raise LlmError("模型返回空内容")
    base = _strip_wrapper(raw)
    candidates = [
        base,
        _normalize_quotes(base),
        _repair_brackets(base),
        _repair_brackets(_normalize_quotes(base)),
        re.sub(r",(\s*[}\]])", r"\1", _repair_brackets(_normalize_quotes(base))),
    ]
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise LlmError(f"无法解析模型输出为 JSON：{raw[:300]}")


# ---------------------------------------------------------------- 角色路由（带缓存）
_ROLE_CACHE: dict[str, tuple[str, float]] = {}  # role -> (config_id, expire_ts)
_ROLE_TTL = 30.0


def get_model_for_role(role: str, fallback: bool = True) -> Optional[ModelConfig]:
    """按 role 解析当前应使用的模型配置。找不到返回 None（调用方需兜底）。

    fallback=True（默认）：role 无人显式认领时，回退到默认连接 / 任意启用连接，
        保证 extract / risk 等用途永不中断。
    fallback=False：仅返回"显式勾选了该 role"的连接。视觉(多模态)等专用用途
        必须严格——文本模型不能替视觉模型兜底，否则会拿纯文本模型去看图而报错。
    """
    now = time.time()
    cache_key = (role, fallback)
    cached = _ROLE_CACHE.get(cache_key)
    if cached and cached[1] > now:
        cfg = _get_by_id(cached[0])
        if cfg and cfg.enabled:
            return cfg

    db = SessionLocal()
    try:
        # 1) 优先：启用且 roles 包含该 role 的配置
        configs = db.query(ModelConfig).filter(ModelConfig.enabled == True).all()
        for c in configs:
            if role in (c.roles or []):
                _ROLE_CACHE[cache_key] = (c.id, now + _ROLE_TTL)
                return c
        if not fallback:
            return None
        # 2) 兜底：启用且标记为默认的配置
        for c in configs:
            if c.is_default:
                _ROLE_CACHE[cache_key] = (c.id, now + _ROLE_TTL)
                return c
        # 3) 再兜底：任意启用的配置
        if configs:
            _ROLE_CACHE[cache_key] = (configs[0].id, now + _ROLE_TTL)
            return configs[0]
    finally:
        db.close()
    return None


def get_default_config() -> Optional[ModelConfig]:
    db = SessionLocal()
    try:
        cfg = db.query(ModelConfig).filter(
            ModelConfig.enabled == True, ModelConfig.is_default == True
        ).first()
        if cfg:
            return cfg
        return db.query(ModelConfig).filter(ModelConfig.enabled == True).first()
    finally:
        db.close()


def _get_by_id(cid: str) -> Optional[ModelConfig]:
    db = SessionLocal()
    try:
        return db.get(ModelConfig, cid)
    finally:
        db.close()


def get_all() -> list[ModelConfig]:
    db = SessionLocal()
    try:
        return db.query(ModelConfig).order_by(ModelConfig.created_at).all()
    finally:
        db.close()


# ---------------------------------------------------------------- 调用实现
def chat_json(
    config: Optional[ModelConfig],
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict | list:
    """统一的 JSON 聊天入口。config 为 None 时抛 LlmError。"""
    if config is None:
        raise LlmError("未配置可用模型：请到「模型配置」添加并启用一个连接，或勾选对应用途")

    model = model or config.model
    timeout = timeout or config.timeout
    temperature = temperature if temperature is not None else config.temperature

    if not model:
        raise LlmError(f"模型连接「{config.name}」未填写模型名")

    if config.provider == PROVIDER_OLLAMA:
        return _ollama_chat(config, prompt, system, model, timeout, temperature)
    if config.provider == PROVIDER_OPENAI:
        return _openai_chat(config, prompt, system, model, timeout, temperature)
    raise LlmError(f"未知提供方 provider={config.provider!r}（应为 ollama / openai）")


def _build_messages(system: Optional[str], prompt: str) -> list[dict]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def chat_json_vision(
    config: Optional[ModelConfig],
    image_b64: str,
    prompt: str,
    system: Optional[str] = None,
    image_media_type: str = "image/png",
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict | list:
    """多模态抽取入口：把图片（base64）直接送给视觉模型，返回解析好的 JSON。

    与 chat_json 的区别：用户消息携带一张图片，由模型"看图"理解，
    不再依赖前置 OCR 把图转成文字。适用于 qwen-vl / llava / GPT-4o / 通义万相等。
    """
    if config is None:
        raise LlmError("未配置可用视觉模型：请到「模型配置」添加一个连接并勾选「视觉抽取(多模态)」")

    model = model or config.model
    timeout = timeout or config.timeout
    temperature = temperature if temperature is not None else config.temperature

    if not model:
        raise LlmError(f"视觉模型连接「{config.name}」未填写模型名")
    if not image_b64:
        raise LlmError("图片内容为空，无法做视觉抽取")

    if config.provider == PROVIDER_OLLAMA:
        return _ollama_vision(config, image_b64, prompt, system, model, timeout, temperature)
    if config.provider == PROVIDER_OPENAI:
        return _openai_vision(config, image_b64, prompt, system, model, timeout, temperature, image_media_type)
    raise LlmError(f"未知提供方 provider={config.provider!r}（应为 ollama / openai）")


def _ollama_vision(
    config: ModelConfig, image_b64: str, prompt: str, system: Optional[str],
    model: str, timeout: int, temperature: float,
) -> dict | list:
    """Ollama 视觉：images 字段直接放 base64 字符串（不带 data URI 前缀）。"""
    url = f"{config.base_url.rstrip('/')}/api/chat"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt, "images": [image_b64]})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException as e:
        raise LlmError(f"调用 Ollama 视觉超时（{timeout}s）：{e}") from e
    except httpx.HTTPStatusError as e:
        raise LlmError(f"Ollama 返回 {e.response.status_code}：{e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise LlmError(f"无法连接 Ollama（{config.base_url}）：{e}") from e
    content = (data.get("message") or {}).get("content", "")
    return parse_json_lenient(content)


def _openai_vision(
    config: ModelConfig, image_b64: str, prompt: str, system: Optional[str],
    model: str, timeout: int, temperature: float, image_media_type: str,
) -> dict | list:
    """OpenAI 兼容视觉：content 用 parts，image_url 带 data URI。"""
    url = _openai_endpoint(config.base_url, "chat/completions")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{image_media_type};base64,{image_b64}"}},
        ],
    })
    payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
    try:
        with httpx.Client(timeout=timeout) as client:
            for attempt, use_rf in enumerate((True, False)):
                p = dict(payload)
                if use_rf:
                    p["response_format"] = {"type": "json_object"}
                try:
                    resp = client.post(url, headers=headers, json=p)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as e:
                    if attempt == 0 and e.response.status_code in (400, 422, 404):
                        continue
                    raise LlmError(f"模型端点返回 {e.response.status_code}：{e.response.text[:200]}") from e
                except httpx.HTTPError as e:
                    raise LlmError(f"无法连接模型端点（{config.base_url}）：{e}") from e
                break
    except LlmError:
        raise
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LlmError(f"OpenAI 兼容端点返回结构异常：{json.dumps(data)[:200]}") from e
    return parse_json_lenient(content)


def _ollama_chat(
    config: ModelConfig, prompt: str, system: Optional[str], model: str, timeout: int, temperature: float
) -> dict | list:
    url = f"{config.base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": _build_messages(system, prompt),
        "stream": False,
        "format": "json",  # 让 Ollama 强制输出 JSON
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException as e:
        raise LlmError(f"调用 Ollama 超时（{timeout}s）：{e}") from e
    except httpx.HTTPStatusError as e:
        raise LlmError(f"Ollama 返回 {e.response.status_code}：{e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise LlmError(f"无法连接 Ollama（{config.base_url}）：{e}。请确认服务可达且模型 {model} 已拉取") from e

    content = (data.get("message") or {}).get("content", "")
    return parse_json_lenient(content)


def _openai_chat(
    config: ModelConfig, prompt: str, system: Optional[str], model: str, timeout: int, temperature: float
) -> dict | list:
    url = _openai_endpoint(config.base_url, "chat/completions")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload = {
        "model": model,
        "messages": _build_messages(system, prompt),
        "temperature": temperature,
        "stream": False,
    }
    # 部分兼容端点不支持 response_format，先带 json_object 试，失败再不带重试
    try:
        with httpx.Client(timeout=timeout) as client:
            for attempt, use_rf in enumerate((True, False)):
                p = dict(payload)
                if use_rf:
                    p["response_format"] = {"type": "json_object"}
                try:
                    resp = client.post(url, headers=headers, json=p)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as e:
                    if attempt == 0 and e.response.status_code in (400, 422, 404):
                        continue  # 该端点不支持 response_format，去掉再试
                    raise LlmError(
                        f"模型端点返回 {e.response.status_code}：{e.response.text[:200]}"
                    ) from e
                except httpx.HTTPError as e:
                    raise LlmError(f"无法连接模型端点（{config.base_url}）：{e}") from e
                break
    except LlmError:
        raise

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LlmError(f"OpenAI 兼容端点返回结构异常：{json.dumps(data)[:200]}") from e
    return parse_json_lenient(content)


# ---------------------------------------------------------------- 远端模型清单
def list_remote_models(config: ModelConfig) -> list[str]:
    """拉取该连接可见的模型名列表（用于前端下拉补全）。失败抛 LlmError。"""
    if config.provider == PROVIDER_OLLAMA:
        url = f"{config.base_url.rstrip('/')}/api/tags"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return [m.get("name", "") for m in resp.json().get("models", [])]
        except httpx.HTTPStatusError as e:
            raise LlmError(f"Ollama 返回 {e.response.status_code}：{e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            raise LlmError(f"无法连接 Ollama（{config.base_url}）：{e}") from e

    if config.provider == PROVIDER_OPENAI:
        url = _openai_endpoint(config.base_url, "models")
        headers = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                return [m.get("id", "") for m in resp.json().get("data", [])]
        except httpx.HTTPStatusError as e:
            raise LlmError(f"端点返回 {e.response.status_code}：{e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            raise LlmError(f"无法连接端点（{config.base_url}）：{e}") from e

    raise LlmError(f"未知提供方 provider={config.provider!r}")


# ---------------------------------------------------------------- 连通性 + 样例自检
def test_model(config: ModelConfig) -> dict:
    """连通性 + 一个最小 JSON 样例调用。返回结构化的自检结果。"""
    result: dict = {
        "provider": config.provider,
        "base_url": config.base_url,
        "model": config.model,
        "reachable": False,
        "models": [],
        "sample_ok": False,
        "latency_ms": None,
        "error": None,
    }
    # 1) 连通性 + 模型清单
    try:
        t0 = time.time()
        models = list_remote_models(config)
        result["reachable"] = True
        result["models"] = models
        result["latency_ms"] = int((time.time() - t0) * 1000)
    except LlmError as e:
        result["error"] = str(e)
        return result

    # 2) 样例 JSON 调用（仅当填了模型名）
    if config.model:
        try:
            t0 = time.time()
            data = chat_json(config, "请只返回一个 JSON 对象：{\"ok\": true}", system="你只输出 JSON，不要解释。")
            result["sample_ok"] = isinstance(data, (dict, list))
            result["latency_ms"] = int((time.time() - t0) * 1000)
        except LlmError as e:
            result["error"] = (result["error"] or "") + f"；样例调用失败：{e}"
    return result
