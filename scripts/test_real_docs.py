"""
scripts/test_real_docs.py — 真实单据端到端抽取测试

对用户提供的一批真实图片（送货单/配送单/发货清单）跑：
  OCR → 模板匹配 → 文本LLM抽取 →（必要时）视觉兜底
输出完整字段值、items 明细、warnings、耗时，便于人工核验抽取质量。

用法：
  python scripts/test_real_docs.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

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


def _extract_with_retry(tpl, text, max_tries=6):
    """OCR→文本LLM 抽取，遇 429/限流指数退避重试。"""
    delays = [5, 10, 20, 35, 60, 90]
    last = None
    for i in range(max_tries):
        oc = extract(tpl, text)
        if oc.success and _filled(oc.fields) > 1:
            return oc, None
        last = oc
        err = (oc.error or "") if oc else "no_result"
        if any(k in err for k in ("429", "rate", "限流", "RateLimit", "Too Many", "exceed")) and i < max_tries - 1:
            time.sleep(delays[i])
            continue
        return oc, (oc.error if oc else None)
    return last, (last.error if last else None)


def _vision_with_retry(tpl, img_path, max_tries=5):
    """视觉直抽，遇 429/限流指数退避重试。"""
    delays = [5, 10, 20, 35, 60]
    last = None
    for i in range(max_tries):
        vis = extract_vision(tpl, img_path)
        if vis.success and _filled(vis.fields) > 1:
            return vis, None
        last = vis
        err = (vis.error or "") if vis else "no_result"
        if any(k in err for k in ("429", "rate", "限流", "RateLimit", "Too Many", "exceed")) and i < max_tries - 1:
            time.sleep(delays[i])
            continue
        return vis, (vis.error if vis else None)
    return last, (last.error if last else None)


def process_one_full(img_path: str) -> dict:
    """对单张真实单据跑完整链路，返回含完整字段值的结果。"""
    t0 = time.time()
    img = str(img_path)
    out = {
        "img": os.path.basename(img),
        "duration_ms": 0,
    }
    db = None
    try:
        db = SessionLocal()
        ocr = ocr_engine.recognize(img)
        out["ocr_engine"] = ocr.engine
        out["ocr_conf"] = round(ocr.avg_confidence, 4) if ocr.avg_confidence is not None else None
        out["ocr_chars"] = len(ocr.text or "")
        out["ocr_text_preview"] = (ocr.text or "")[:500].replace("\n", " ")

        tpl = tpl_mod.match_template(db, ocr.text, os.path.splitext(img)[1])
        out["matched"] = tpl.name if tpl else None
        if tpl is None:
            out["error"] = "no_template"
            return out

        out["template"] = tpl.name
        out["template_scenario"] = tpl.scenario
        out["template_display_style"] = tpl.display_style

        # 路线 A：OCR → 文本 LLM
        oc, last_err = _extract_with_retry(tpl, ocr.text)
        out["ocr_extract_ok"] = bool(oc and oc.success)
        out["ocr_extract_conf"] = round(oc.confidence, 3) if (oc and oc.confidence is not None) else None
        out["ocr_filled"] = _filled(oc.fields) if oc else 0
        out["ocr_error"] = last_err
        out["ocr_fields"] = oc.fields if oc else {}
        out["ocr_warnings"] = oc.warnings if oc else []
        out["ocr_model"] = oc.model if oc else ""

        # 视觉兜底判定（与 pipeline 同逻辑）
        fb_enabled = bool(getattr(settings, "VISION_EXTRACT_FALLBACK_ENABLED", True))
        fb_conf = float(getattr(settings, "VISION_EXTRACT_FALLBACK_CONF", 0.85))
        low = (ocr.avg_confidence is None or ocr.avg_confidence < fb_conf)
        ocr_failed = not oc.success
        ocr_few = oc.success and out["ocr_filled"] <= 1
        out["fallback_trigger"] = bool(low or ocr_failed or ocr_few)

        # 路线 B：视觉直抽
        if fb_enabled and out["fallback_trigger"]:
            vis, vis_err = _vision_with_retry(tpl, img)
            out["vision_ok"] = vis.success if vis else False
            out["vision_conf"] = round(vis.confidence, 3) if (vis and vis.confidence is not None) else None
            out["vision_filled"] = _filled(vis.fields) if vis else 0
            out["vision_fields"] = vis.fields if vis else {}
            out["vision_warnings"] = vis.warnings if vis else []
            out["vision_error"] = vis_err
            out["vision_model"] = vis.model if vis else ""
            adopt = (
                vis.success
                and (ocr_failed or ocr_few or (vis.confidence or 0) >= (oc.confidence or 0))
                and out["vision_filled"] >= out["ocr_filled"]
            )
            out["method"] = "vision" if adopt else "ocr"
            out["winner"] = "vision" if (adopt and out["vision_filled"] >= out["ocr_filled"]) else "ocr"
            out["final_fields"] = (vis.fields if vis else {}) if adopt else (oc.fields if oc else {})
        else:
            out["method"] = "ocr"
            out["winner"] = "ocr"
            out["final_fields"] = oc.fields if oc else {}

        # 最终采用的 warnings
        if out["winner"] == "vision":
            out["final_warnings"] = out.get("vision_warnings", [])
        else:
            out["final_warnings"] = out.get("ocr_warnings", [])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    out["duration_ms"] = int((time.time() - t0) * 1000)
    return out


def _copy_sources(sources: list[str], dest_dir: Path) -> list[str]:
    """把源图片复制到项目目录，返回新路径列表。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for i, src in enumerate(sources, 1):
        src_path = Path(src)
        if not src_path.exists():
            print(f"⚠️ 文件不存在，跳过: {src}")
            continue
        ext = src_path.suffix.lower() or ".jpg"
        dst = dest_dir / f"real_{i:02d}{ext}"
        shutil.copy2(str(src_path), str(dst))
        copied.append(str(dst))
    return copied


def _summarize(rows: list[dict]) -> str:
    lines = []
    lines.append("# 真实单据抽取测试报告")
    lines.append("")
    lines.append(f"- 样本数：**{len(rows)}**")
    lines.append(f"- 测试时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # 汇总表
    lines.append("## 一、总体汇总")
    lines.append("")
    lines.append("| 图片 | 匹配模板 | OCR置信 | 采用方式 | 字段填充 | 明细行数 | 告警 | 耗时(ms) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        items = r.get("final_fields", {}).get("items", [])
        item_count = len(items) if isinstance(items, list) else "-"
        warnings = r.get("final_warnings", [])
        warn_s = "；".join(warnings) if warnings else "无"
        lines.append(
            f"| {r.get('img')} | {r.get('matched', '-')} | {r.get('ocr_conf', '-')} | "
            f"{r.get('winner', '-')} | {r.get('ocr_filled', 0)}/{len(r.get('ocr_fields', {}))} "
            f"({r.get('vision_filled', '-') if r.get('vision_filled') is not None else '-'} 视觉) | "
            f"{item_count} | {warn_s} | {r.get('duration_ms', '-')} |"
        )
    lines.append("")

    # 每张详情
    lines.append("## 二、逐单详情")
    for idx, r in enumerate(rows, 1):
        lines.append("")
        lines.append(f"### 2.{idx} {r.get('img')}")
        lines.append("")
        lines.append(f"- **匹配模板**：{r.get('matched', '-')}（{r.get('template_scenario', '-')}）")
        lines.append(f"- **OCR引擎**：{r.get('ocr_engine', '-')}，置信：{r.get('ocr_conf', '-')}")
        lines.append(f"- **采用方式**：{r.get('winner', '-')}")
        if r.get("ocr_model"):
            lines.append(f"- **文本模型**：{r.get('ocr_model')}")
        if r.get("vision_model"):
            lines.append(f"- **视觉模型**：{r.get('vision_model')}")
        lines.append("")

        lines.append("**OCR 文本预览：**")
        lines.append("```")
        lines.append(r.get("ocr_text_preview", "")[:300])
        lines.append("```")
        lines.append("")

        final = r.get("final_fields", {})
        items = final.get("items", [])
        if isinstance(items, list) and items:
            lines.append("**物料明细（items）：**")
            lines.append("")
            lines.append("| # | 名称 | 编码 | 规格 | 单位 | 数量 | 单价 | 金额 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for i, row in enumerate(items, 1):
                if isinstance(row, dict):
                    lines.append(
                        f"| {i} | {row.get('name', '')} | {row.get('code', '')} | "
                        f"{row.get('spec', '')} | {row.get('unit', '')} | {row.get('qty', '')} | "
                        f"{row.get('unit_price', '')} | {row.get('amount', '')} |"
                    )
                else:
                    lines.append(f"| {i} | {row} | | | | | | |")
            lines.append("")

        lines.append("**顶层字段：**")
        lines.append("```json")
        top = {k: v for k, v in final.items() if k != "items"}
        lines.append(json.dumps(top, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")

        warnings = r.get("final_warnings", [])
        if warnings:
            lines.append("**⚠️ 告警：**")
            for w in warnings:
                lines.append(f"- {w}")
            lines.append("")

        if r.get("ocr_error"):
            lines.append(f"- OCR抽取错误：{r['ocr_error']}")
        if r.get("vision_error"):
            lines.append(f"- 视觉抽取错误：{r['vision_error']}")
        if r.get("error"):
            lines.append(f"- 处理异常：{r['error']}")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=2.0, help="每张之间的间隔秒数")
    args = ap.parse_args()

    # 用户本次给出的 5 张真实单据（剪贴板路径）
    sources = [
        r"C:\Users\Alan\.workbuddy\clipboard-images\clipboard-2026-08-17T08-15-08-115Z-887a4b8f.jpg",
        r"C:\Users\Alan\.workbuddy\clipboard-images\clipboard-2026-08-17T08-15-08-121Z-382e5a0a.jpg",
        r"C:\Users\Alan\.workbuddy\clipboard-images\clipboard-2026-08-17T08-15-08-126Z-e2891bd2.jpg",
        r"C:\Users\Alan\.workbuddy\clipboard-images\clipboard-2026-08-17T08-15-08-131Z-966c203d.png",
        r"C:\Users\Alan\.workbuddy\clipboard-images\clipboard-2026-08-17T08-15-08-136Z-a86e4d13.png",
    ]

    dest_dir = Path(ROOT) / "data" / "real_docs_test"
    copied = _copy_sources(sources, dest_dir)
    print(f"已复制 {len(copied)}/{len(sources)} 张图片到 {dest_dir}")

    rows = []
    for p in copied:
        print(f"\n▶ 处理 {os.path.basename(p)} ...")
        r = process_one_full(p)
        rows.append(r)
        print(f"  模板={r.get('matched')} 方式={r.get('winner')} 填充={r.get('ocr_filled')} "
              f"耗时={r.get('duration_ms')}ms")
        if r.get("final_warnings"):
            for w in r["final_warnings"]:
                print(f"  ⚠️ {w}")
        time.sleep(args.gap)

    # 落盘
    jsonl_path = dest_dir / "rows.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    report = _summarize(rows)
    report_path = Path(ROOT) / "data" / "real_docs_test_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ 完成。JSONL: {jsonl_path}\n报告: {report_path}")


if __name__ == "__main__":
    main()
