"""
scripts/test_new_model.py — 对比测试"新模型"(智谱 glm-4.6) 与 本地 qwen2.5:14b
在真实业务任务上的效果：结构化抽取 + 风险语义研判。

复用项目自有的 prompt 构建逻辑（extractor._build_prompt / detector._LLM_SYSTEM），
但显式传入 ModelConfig，绕开 role 解析，从而干净对比两个模型本身的能力。
"""
from __future__ import annotations

import sys, time, json, types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from app.services.llm.client import chat_json, ModelConfig
from app.services.extract.extractor import _build_prompt, SYSTEM_PROMPT
from app.services.risk.detector import _LLM_SYSTEM

ENGINE = create_engine(f"sqlite:///{(ROOT / 'data' / 'archive.db').as_posix()}")

# ---- 取真实配置 ----
def get_cfg(cid):
    with ENGINE.connect() as c:
        r = c.execute(text(
            "SELECT id,name,provider,base_url,api_key,model,timeout FROM model_config WHERE id=:id"
        ), {"id": cid}).fetchone()
    return ModelConfig(
        id=r.id, name=r.name, provider=r.provider, base_url=r.base_url,
        api_key=r.api_key or "", model=r.model, timeout=r.timeout or 180,
        temperature=0.1,
    )

CFG_ZHIPU = get_cfg("deepseek-ai")      # 智谱 glm-4.6（新模型，当前生效）
CFG_OLLAMA = get_cfg("local-ollama")    # 本地 qwen2.5:14b


def run(cfg, prompt, system=None):
    t0 = time.time()
    try:
        out = chat_json(cfg, prompt, system=system)
        ms = int((time.time() - t0) * 1000)
        return out, ms, None
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return None, ms, repr(e)


def trunc(s, n=160):
    s = json.dumps(s, ensure_ascii=False) if not isinstance(s, str) else s
    return s if len(s) <= n else s[:n] + "…"


print("=" * 70)
print("任务 A：结构化抽取（真实 OCR 送货单，命中「送货单」模板）")
print("=" * 70)

# 真实 OCR 文本（取自 ocr_result，含 OCR 噪声：草甘麟/321600.0p）
with ENGINE.connect() as c:
    ocr_text = c.execute(text("SELECT text_content FROM ocr_result LIMIT 1")).fetchone()[0]

# 送货单 模板字段
tpl = types.SimpleNamespace(
    name="送货单",
    fields_schema=[
        {"key": "delivery_no", "label": "送货单号", "type": "string"},
        {"key": "delivery_date", "label": "送货日期", "type": "date"},
        {"key": "supplier", "label": "供应商", "type": "string"},
        {"key": "receiver", "label": "收货单位", "type": "string"},
        {"key": "total_amount", "label": "合计金额", "type": "number"},
        {"key": "items", "label": "物料明细", "type": "array"},
    ],
    prompt_extra="金额字段一律输出纯数字。数量与单价相乘应等于金额，若不符以表格原值为准。",
)

prompt_a = _build_prompt(tpl, ocr_text)

for tag, cfg in (("智谱 glm-4.6", CFG_ZHIPU), ("本地 qwen2.5:14b", CFG_OLLAMA)):
    out, ms, err = run(cfg, prompt_a, system=SYSTEM_PROMPT)
    print(f"\n--- {tag}  [{ms} ms] ---")
    if err:
        print("  错误:", err)
    else:
        if isinstance(out, dict) and "fields" in out:
            print("  字段:", json.dumps(out["fields"], ensure_ascii=False))
            print("  置信度:", out.get("confidence"))
        else:
            print("  输出:", trunc(out, 400))

print("\n标准答案(人工从OCR提取):")
print('  delivery_no=SH20260804001, delivery_date=2026-08-04,')
print('  supplier=广康生化科技股份有限公司, receiver=晟康生产基地,')
print('  total_amount=376215.0, items=3项(草甘膦原药/助剂A/包装桶)')


print("\n" + "=" * 70)
print("任务 B：风险语义研判（正确形态：_LLM_SYSTEM 作 system，消息文本作 prompt）")
print("=" * 70)

messages = [
    ("隐晦飞单话术", "这单咱们私底下处理就行，别走系统了，我直接转你个人卡，别让公司知道"),
    ("正常业务消息", "收到，已安排明天上午送货，单号 SH20260804001 麻烦确认一下"),
]

for label, msg in messages:
    print(f"\n### 消息[{label}]: {msg}")
    for tag, cfg in (("智谱 glm-4.6", CFG_ZHIPU), ("本地 qwen2.5:14b", CFG_OLLAMA)):
        out, ms, err = run(cfg, msg, system=_LLM_SYSTEM)
        print(f"  - {tag} [{ms} ms]:", end=" ")
        if err:
            print("错误", err)
        else:
            print(trunc(out, 200))

print("\n完成。")
