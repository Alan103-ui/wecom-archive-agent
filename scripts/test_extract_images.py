"""
scripts/test_extract_images.py — 结构化抽取「多场景/多格式」压力测试（驱动版）

逐张图调用 scripts/_ocr_one.py 子进程跑 OCR+模板匹配(+可选LLM)，
子进程崩溃不影响其他图；结果追加写入 data/ocr_rows.jsonl，最后生成 Markdown 报告。

用法：
  python scripts/test_extract_images.py
  python scripts/test_extract_images.py --with-llm
  python scripts/test_extract_images.py --doc compare
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "data" / "sample_images"
RESULT_JSONL = ROOT / "data" / "ocr_rows.jsonl"
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-llm", action="store_true")
    ap.add_argument("--doc", default=None)
    args = ap.parse_args()

    if not SAMPLE_DIR.exists():
        print(f"未找到样例图目录 {SAMPLE_DIR}，请先运行 gen_sample_images.py")
        return
    if RESULT_JSONL.exists():
        RESULT_JSONL.unlink()

    total = 0
    for doc_dir in sorted(SAMPLE_DIR.iterdir()):
        if not doc_dir.is_dir():
            continue
        if args.doc and doc_dir.name != args.doc:
            continue
        for img in sorted(doc_dir.iterdir()):
            if img.suffix.lower() not in (".png", ".jpg", ".jpeg", ".pdf"):
                continue
            total += 1
            print(f"[处理 {total}] {doc_dir.name}/{img.name}", flush=True)
            cmd = [str(PY), str(ROOT / "scripts" / "_ocr_one.py"), str(img)]
            if args.with_llm:
                cmd.append("--with-llm")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                      cwd=str(ROOT))
                if proc.returncode != 0 or not proc.stdout.strip():
                    row = {"doc": doc_dir.name, "variant": img.stem, "ext": img.suffix.lower().lstrip("."),
                           "engine": "CRASH", "template": f"rc={proc.returncode}",
                           "raw_chars": "-", "raw_conf": "-", "pp_chars": "-",
                           "pp_conf": "-", "final_chars": "-", "avg_conf": "-", "coverage": "-"}
                    if proc.stderr.strip():
                        row["template"] = "crash:" + proc.stderr.strip()[:30]
                else:
                    row = json.loads(proc.stdout.strip().splitlines()[-1])
            except subprocess.TimeoutExpired:
                row = {"doc": doc_dir.name, "variant": img.stem, "ext": img.suffix.lower().lstrip("."),
                       "engine": "TIMEOUT", "template": "-", "raw_chars": "-", "raw_conf": "-",
                       "pp_chars": "-", "pp_conf": "-", "final_chars": "-", "avg_conf": "-", "coverage": "-"}
            with RESULT_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()

    print(f"\n完成 {total} 个样例，结果写入 {RESULT_JSONL}", flush=True)
    _render_markdown()


def _render_markdown():
    if not RESULT_JSONL.exists():
        return
    rows = [json.loads(l) for l in RESULT_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
    headers = ["文档", "变体", "格式", "引擎", "原始字数", "原始置信", "预处理字数",
               "预处理置信", "最终字数", "最终置信", "命中模板", "字段覆盖"]
    out = ["\n# 结构化抽取多场景测试报告\n",
           "| " + " | ".join(headers) + " |",
           "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(h, "-")) for h in
                   ["doc", "variant", "ext", "engine", "raw_chars", "raw_conf",
                    "pp_chars", "pp_conf", "final_chars", "avg_conf", "template", "coverage"]) + " |")
    imp = [r for r in rows if r.get("raw_conf") not in ("-",) and r.get("pp_conf") not in ("-",)]
    if imp:
        up = sum(1 for r in imp if float(r["pp_conf"]) > float(r["raw_conf"]))
        out.append(f"\n预处理提升置信的样例数：{up}/{len(imp)}")
    out.append(f"\n总计 {len(rows)} 个样例。")
    (ROOT / "data" / "ocr_report.md").write_text("\n".join(out), encoding="utf-8")
    print(f"报告已生成：data/ocr_report.md", flush=True)


if __name__ == "__main__":
    main()
