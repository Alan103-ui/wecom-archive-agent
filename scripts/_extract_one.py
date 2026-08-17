"""
scripts/_extract_one.py — 单图「OCR→抽取→视觉兜底」处理单元（可复用）

抽成 process_one(img_path) 函数，供 test_unified.py 的进程池并发调用，
也兼容独立 subprocess 调用（python _extract_one.py <图片路径>）。

逻辑与 app/services/pipeline.py 的抽取决策一致：
  1. OCR 取文本+置信度，模板匹配
  2. 走 OCR→文本LLM 抽取
  3. 若 OCR 置信度<阈值 / OCR抽取失败 / 字段极少，且视觉模型可用 → 视觉直抽兜底

设计要点（为进程池/多进程优化）：
  · 所有重型 import 延迟到 process_one 内部，加速子进程 spawn，且避免顶层 import 拖慢池创建
  · 单图异常被 try/except 兜住，返回 {"error": ...} 而非抛异常炸掉整个 worker 进程
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config import settings  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.services.extract import templates as tpl_mod  # noqa: E402
from app.services.extract.extractor import extract, extract_vision  # noqa: E402
from app.services.ocr import engine as ocr_engine  # noqa: E402


def _filled(fields):
    return sum(1 for v in (fields or {}).values() if v not in (None, "", [], {}))


def _extract_with_retry(tpl, text, max_tries=4):
    """OCR→文本LLM 抽取，专治远程模型 429 限流：限流则指数退避重试，其他错误立即返回。"""
    delays = [2, 4, 8, 15]
    last = None
    for i in range(max_tries):
        oc = extract(tpl, text)
        if oc.success and _filled(oc.fields) > 1:
            return oc, None
        last = oc
        err = (oc.error or "") if oc else "no_result"
        if ("429" in err or "速率限制" in err) and i < max_tries - 1:
            time.sleep(delays[i])
            continue
        return oc, (oc.error if oc else None)
    return last, (last.error if last else None)


def process_one(img_path: str) -> dict:
    """对单张图跑完整「OCR→抽取→视觉兜底」链路，返回结果 dict（绝不抛异常）。"""
    from app.config import settings  # noqa: E402

    t0 = time.time()
    img = str(img_path)
    out = {
        "img": os.path.basename(img),
        "doc": os.path.basename(os.path.dirname(img)),
        "duration_ms": 0,
    }
    db = None
    try:
        db = SessionLocal()
        ocr = ocr_engine.recognize(img)
        out["ocr_engine"] = ocr.engine
        out["ocr_conf"] = round(ocr.avg_confidence, 4) if ocr.avg_confidence is not None else None
        out["ocr_chars"] = len(ocr.text or "")

        tpl = tpl_mod.match_template(db, ocr.text, os.path.splitext(img)[1])
        out["matched"] = tpl.name if tpl else None
        if tpl is None:
            out["error"] = "no_template"
            return out

        # 路线 A：OCR → 文本 LLM（429 限流时指数退避重试，规避远程模型偶发限流造成的误触发）
        oc, last_err = _extract_with_retry(tpl, ocr.text)
        out["ocr_extract_ok"] = bool(oc and oc.success)
        out["ocr_extract_conf"] = round(oc.confidence, 3) if (oc and oc.confidence is not None) else None
        out["ocr_filled"] = _filled(oc.fields) if oc else 0
        out["ocr_error"] = last_err

        # 视觉兜底判定（与 pipeline 同逻辑）
        fb_enabled = bool(getattr(settings, "VISION_EXTRACT_FALLBACK_ENABLED", True))
        fb_conf = float(getattr(settings, "VISION_EXTRACT_FALLBACK_CONF", 0.85))
        low = (ocr.avg_confidence is None or ocr.avg_confidence < fb_conf)
        ocr_failed = not oc.success
        ocr_few = oc.success and out["ocr_filled"] <= 1
        out["fallback_trigger"] = bool(low or ocr_failed or ocr_few)

        # 路线 B：视觉直抽（仅当兜底可能触发时才调用，省成本）
        if fb_enabled and out["fallback_trigger"]:
            vis = extract_vision(tpl, img)
            out["vision_ok"] = vis.success
            out["vision_conf"] = round(vis.confidence, 3) if vis.confidence is not None else None
            out["vision_filled"] = _filled(vis.fields)
            adopt = vis.success and (ocr_failed or ocr_few or (vis.confidence or 0) >= (oc.confidence or 0))
            out["method"] = "vision" if adopt else "ocr"
            out["winner"] = "vision" if (adopt and out["vision_filled"] >= out["ocr_filled"]) else "ocr"
        else:
            out["method"] = "ocr"
            out["winner"] = "ocr"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    out["duration_ms"] = int((time.time() - t0) * 1000)
    return out


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: _extract_one.py <图片路径>"}, ensure_ascii=False))
        return
    out = process_one(sys.argv[1])
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
