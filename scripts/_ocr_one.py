"""
scripts/_ocr_one.py — 单图 OCR 测试子进程（被 test_extract_images.py 调用）

对单张图跑：OCR(raw vs 预处理) + 模板匹配 + 可选 LLM 抽取，
输出一行 JSON 到 stdout（父进程收集）。独立进程运行，崩溃不影响其他图。

用法：python scripts/_ocr_one.py <图片路径> [--with-llm]
"""
import argparse
import json
import sys

sys.path.insert(0, ".")

import numpy as np
from PIL import Image

from app.db.database import SessionLocal
from app.models.entities import ExtractTemplate
from app.services.ocr import engine as ocr_engine
from app.services.extract import templates as tpl_mod
from app.services.extract.extractor import extract as llm_extract

VARIANT_LABEL = {
    "clear": "清晰", "blur": "模糊", "lowcontrast": "低对比",
    "rotate": "旋转90°", "handwritten": "手写体",
}


def _ocr_raw_vs_pp(path):
    raw = np.array(Image.open(path).convert("RGB"))
    b1, a1 = ocr_engine._run_rapidocr(raw)
    pp = ocr_engine._preprocess_for_ocr(raw)
    b2, a2 = ocr_engine._run_rapidocr(np.array(pp))
    return ("\n".join(x.text for x in b1), a1,
            "\n".join(x.text for x in b2), a2)


def _coverage(tpl, fields):
    keys = [f["key"] for f in (tpl.fields_schema or []) if f.get("key")]
    if not keys:
        return 0, 0
    return sum(1 for k in keys if fields.get(k) not in (None, "", [], {})), len(keys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--with-llm", action="store_true")
    args = ap.parse_args()
    path = args.path
    from pathlib import Path
    img = Path(path)
    doc = img.parent.name
    variant = img.stem
    ext = img.suffix.lower().lstrip(".")
    is_img = ext in ("png", "jpg", "jpeg")

    row = {
        "doc": doc, "variant": VARIANT_LABEL.get(variant, variant),
        "ext": ext, "engine": "-", "raw_chars": "-", "raw_conf": "-",
        "pp_chars": "-", "pp_conf": "-", "final_chars": "-",
        "avg_conf": "-", "template": "-", "coverage": "-",
    }
    db = SessionLocal()
    try:
        final = ocr_engine.recognize(path)
        row["engine"] = final.engine
        row["final_chars"] = len(final.text)
        row["avg_conf"] = f"{final.avg_confidence:.2f}" if final.avg_confidence is not None else "-"
        if is_img:
            rt, rc_, pt, pc_ = _ocr_raw_vs_pp(img)
            row["raw_chars"], row["raw_conf"] = len(rt), f"{rc_:.2f}" if rc_ is not None else "-"
            row["pp_chars"], row["pp_conf"] = len(pt), f"{pc_:.2f}" if pc_ is not None else "-"
        tpl = tpl_mod.match_template(db, final.text, ext)
        row["template"] = tpl.name if tpl else "-"
        if args.with_llm and tpl is not None:
            oc = llm_extract(tpl, final.text)
            if oc.success:
                n, t = _coverage(tpl, oc.fields)
                row["coverage"] = f"{n}/{t} ({oc.confidence if oc.confidence is not None else '-'})"
            else:
                row["coverage"] = f"失败:{str(oc.error)[:18]}"
    except Exception as e:
        row["engine"] = "EXC"
        row["template"] = f"err:{str(e)[:18]}"
    finally:
        db.close()
    print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
