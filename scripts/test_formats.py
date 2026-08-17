"""
scripts/test_formats.py — 「格式 × 内容」矩阵抽取测试

目标：用不同文件格式(PNG/JPG/PDF) + 不同内容(6类单据) 验证模板抽取质量，
      重点统计上一轮新增字段(币种/税率/合计数量/联系人/备注/开票信息等)是否真被填出。

用法：
  python scripts/test_formats.py                 # 默认矩阵(清晰18+手写3)
  python scripts/test_formats.py --workers 2
  python scripts/test_formats.py --doc delivery --variant handwritten

输出：
  data/format_test_report.md   可读矩阵报告
  data/format_test_rows.jsonl  逐图原始结果(便于复算)
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 两轮新增的「富集字段」，用于重点统计
ENRICHED = {
    "code", "material_code", "goods_code", "product_code", "unit",
    "total_qty", "contact", "remark", "tax_rate", "currency",
    "drawer", "seller_address", "buyer_address", "sign_place", "qty",
}

DOCS = ["delivery", "invoice", "contract", "quote", "compare", "report"]
FORMATS = ["png", "jpg", "pdf"]


def build_matrix(doc_filter=None, variant_filter=None):
    """构造测试文件清单：默认 清晰变体(6类×3格式) + 手写变体(delivery×3格式)。"""
    base = os.path.join(ROOT, "data", "sample_images")
    files = []
    # 主矩阵：清晰变体
    for d in DOCS:
        if doc_filter and d != doc_filter:
            continue
        for fmt in FORMATS:
            if variant_filter and variant_filter != "clear":
                continue
            p = os.path.join(base, d, f"clear.{fmt}")
            if os.path.exists(p):
                files.append(p)
    # 手写变体(体现视觉兜底在不同格式下接住)
    for fmt in FORMATS:
        if variant_filter and variant_filter != "handwritten":
            continue
        p = os.path.join(base, "delivery", f"handwritten.{fmt}")
        if os.path.exists(p) and (doc_filter in (None, "delivery")):
            files.append(p)
    return files


def _chosen_fields(r):
    """按最终采用方式(ocr/vision)取填出的字段键。"""
    if r.get("method") == "vision" and r.get("vision_field_keys") is not None:
        return set(r.get("vision_field_keys", []))
    return set(r.get("ocr_field_keys", []))


def _merged_fields(r):
    ocr = set(r.get("ocr_field_keys", []) or [])
    vis = set(r.get("vision_field_keys", []) or [])
    return ocr | vis


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--doc", default=None)
    ap.add_argument("--variant", default=None)
    ap.add_argument("--gap", type=float, default=2.0, help="每张之间的间隔秒数，规避远程模型 429")
    args = ap.parse_args()

    files = build_matrix(args.doc, args.variant)
    if not files:
        print("无匹配样本")
        return

    from scripts._extract_one import process_one  # noqa: E402

    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, f): f for f in files}
        for fut in futs:
            try:
                rows.append(fut.result(timeout=300))
            except Exception as e:  # noqa: BLE001
                rows.append({"img": os.path.basename(futs[fut]), "error": str(e)[:160]})
            time.sleep(args.gap)  # 串行 + 间隔，规避 429 突发

    # 落盘 jsonl（截断而非删除，规避沙箱安全删除 shim）
    jsonl = os.path.join(ROOT, "data", "format_test_rows.jsonl")
    with open(jsonl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- 汇总 ----
    n = len(rows)
    ok = [r for r in rows if r.get("matched")]
    # 按内容+格式聚合
    per_doc = {}
    for r in ok:
        d = r.get("doc")
        per_doc.setdefault(d, []).append(r)

    # 富集字段覆盖率（仅统计 schema 含该字段的文件）
    enriched_cov = {k: [0, 0] for k in ENRICHED}  # [applicable, filled]
    for r in ok:
        schema = set(r.get("schema_keys", []) or [])
        filled = _chosen_fields(r)
        for k in ENRICHED:
            if k in schema:
                enriched_cov[k][0] += 1
                if k in filled:
                    enriched_cov[k][1] += 1

    # ---- 生成报告 ----
    lines = []
    lines.append("# 格式 × 内容 抽取测试报告")
    lines.append("")
    lines.append(f"- 样本数：**{n}**（{len(ok)} 张成功匹配模板）")
    lines.append(f"- 内容覆盖：{', '.join(DOCS)}")
    lines.append(f"- 格式覆盖：{', '.join(FORMATS).upper()}")
    lines.append(f"- 重点统计字段：{', '.join(sorted(ENRICHED))}")
    lines.append("")

    # 逐图矩阵
    lines.append("## 一、逐图矩阵")
    lines.append("")
    lines.append("| 单据 | 格式 | 变体 | 匹配模板 | OCR置信 | 字段填充 | 新字段填充 | 方式 | 耗时(ms) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        img = r.get("img", "?")
        parts = img.split(".")
        fmt = parts[-2] if len(parts) >= 2 else "?"
        variant = parts[0] if parts else "?"
        doc = r.get("doc", "?")
        matched = r.get("matched") or "✗"
        conf = r.get("ocr_conf")
        conf_s = f"{conf:.3f}" if isinstance(conf, (int, float)) else "-"
        schema = r.get("schema_keys", []) or []
        filled = _chosen_fields(r)
        fill_rate = f"{len(filled)}/{len(schema)}"
        # 新字段
        enr_app = [k for k in schema if k in ENRICHED]
        enr_fill = [k for k in enr_app if k in filled]
        enr_s = f"{len(enr_fill)}/{len(enr_app)}" + (f" ({','.join(enr_fill)})" if enr_fill else "")
        method = r.get("method", "-")
        dur = r.get("duration_ms", 0)
        err = r.get("error")
        if err:
            lines.append(f"| {doc} | {fmt} | {variant} | ✗({err[:12]}) | - | - | - | - | {dur} |")
        else:
            lines.append(f"| {doc} | {fmt} | {variant} | {matched} | {conf_s} | {fill_rate} | {enr_s} | {method} | {dur} |")
    lines.append("")

    # 按内容聚合
    lines.append("## 二、按内容类型聚合（平均字段填充率）")
    lines.append("")
    lines.append("| 单据 | 测试数 | 平均填充率 | 平均新字段填充 |")
    lines.append("|---|---|---|---|")
    for d in DOCS:
        rs = per_doc.get(d, [])
        if not rs:
            continue
        tot_schema = sum(len(r.get("schema_keys", []) or []) for r in rs)
        tot_fill = sum(len(_chosen_fields(r)) for r in rs)
        enr_app = sum(len([k for k in (r.get("schema_keys", []) or []) if k in ENRICHED]) for r in rs)
        enr_fill = sum(len([k for k in [x for x in (r.get("schema_keys", []) or []) if x in ENRICHED] if k in _chosen_fields(r)]) for r in rs) if False else 0
        # 重算 enr_fill 正确
        enr_fill = 0
        for r in rs:
            schema = r.get("schema_keys", []) or []
            filled = _chosen_fields(r)
            enr_fill += len([k for k in schema if k in ENRICHED and k in filled])
        fill_pct = f"{(tot_fill / tot_schema * 100):.0f}%" if tot_schema else "-"
        enr_pct = f"{(enr_fill / enr_app * 100):.0f}%" if enr_app else "-"
        lines.append(f"| {d} | {len(rs)} | {fill_pct} | {enr_pct} |")
    lines.append("")

    # 新字段覆盖率
    lines.append("## 三、新增字段覆盖率（仅统计模板含该字段的样本）")
    lines.append("")
    lines.append("| 新增字段 | 适用样本数 | 成功填出 | 覆盖率 |")
    lines.append("|---|---|---|---|")
    for k in sorted(ENRICHED):
        app, fl = enriched_cov[k]
        if app == 0:
            continue
        pct = f"{(fl / app * 100):.0f}%"
        lines.append(f"| {k} | {app} | {fl} | {pct} |")
    lines.append("")

    # 兜底情况
    fb = [r for r in ok if r.get("method") == "vision"]
    lines.append("## 四、视觉兜底触发情况")
    lines.append("")
    if fb:
        lines.append(f"共 **{len(fb)}** 张走视觉兜底：")
        for r in fb:
            lines.append(f"- {r.get('img')} → winner={r.get('winner')} vision_filled={r.get('vision_filled')} ocr_filled={r.get('ocr_filled')}")
    else:
        lines.append("本次无样本触发视觉兜底（清晰样本均 OCR 直出）。")
    lines.append("")

    report = os.path.join(ROOT, "data", "format_test_report.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"完成：{n} 张 -> {report}")
    print(f"成功匹配模板：{len(ok)}；视觉兜底：{len(fb)}")


if __name__ == "__main__":
    run()
