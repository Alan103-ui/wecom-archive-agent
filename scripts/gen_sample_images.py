"""
scripts/gen_sample_images.py — 生成「结构化抽取」多场景压力测试图

覆盖维度：
  · 文档类型：送货单 / 增值税发票 / 采购比价单 / 生产日报(list) / 报价单(table+明细数组) / 采购合同(card)
  · 样式变体：clear(清晰) / blur(模糊) / lowcontrast(低对比) / rotate(旋转90°) / handwritten(楷体手写观感)
  · 文件格式：png / jpg / pdf

输出目录：data/sample_images/<doc>/<variant>.<ext>

用途：配合 test_extract_images.py 跑 RapidOCR + 模板匹配 + (可选)LLM 抽取，
      验证不同样式/格式/清晰度的识别质量，作为系统优化的基线。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 让脚本能 import 项目包（app.*）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont, ImageFilter  # noqa: E402

OUT_ROOT = ROOT / "data" / "sample_images"

# 字体：黑体用于印刷体，楷体用于手写观感
HEI = "C:/Windows/Fonts/simhei.ttf"
KAI = "C:/Windows/Fonts/simkai.ttf"


def _font(size, handwritten=False):
    path = KAI if handwritten else HEI
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _new_canvas(w=1000, h=1400):
    img = Image.new("RGB", (w, h), "white")
    return img, ImageDraw.Draw(img)


def _draw_lines(draw, lines, x=60, y=60, font=None, fill="black", lh=46):
    """lines: list[str]，逐行绘制。"""
    f = font or _font(28)
    for ln in lines:
        draw.text((x, y), ln, font=f, fill=fill)
        y += lh
    return y


def _draw_table(draw, headers, rows, x=60, y=60, font=None, fill="black",
                col_w=None, row_h=44, lh=34):
    f = font or _font(22)
    cw = col_w or [180] * len(headers)
    # 表头
    cx = x
    for h, w_ in zip(headers, cw):
        draw.text((cx, y), str(h), font=f, fill=fill)
        cx += w_
    y += row_h
    # 分隔线
    draw.line([(x, y - 8), (x + sum(cw), y - 8)], fill=fill, width=1)
    # 行
    for r in rows:
        cx = x
        for val, w_ in zip(r, cw):
            draw.text((cx, y), str(val), font=f, fill=fill)
            cx += w_
        y += row_h
    return y


# ---------------------------------------------------------------- 三类单据内容
def render_delivery(draw, handwritten=False):
    f_h = _font(40, handwritten)
    f = _font(26, handwritten)
    fill = "black"
    draw.text((60, 40), "送货单", font=f_h, fill=fill)
    y = 120
    y = _draw_lines(draw, [
        "送货单号：SH20260804001",
        "送货日期：2026-08-04",
        "供应商：广东广康化工实业有限公司",
        "收货单位：广州材料仓库",
        "合计金额：86500",
    ], y=y, font=f, fill=fill, lh=44)
    y += 10
    _draw_table(draw, ["物料名称", "规格", "数量", "单价", "金额"], [
        ["碳酸氢钠", "工业一级 25kg/袋", "500", "120", "60000"],
        ["聚合氯化铝", "28% 30kg/袋", "100", "265", "26500"],
    ], x=60, y=y, font=_font(20, handwritten), fill=fill,
        col_w=[240, 260, 120, 140, 160], row_h=42)


def render_invoice(draw, handwritten=False):
    f_h = _font(36, handwritten)
    f = _font(24, handwritten)
    fill = "black"
    draw.text((60, 40), "增值税专用发票", font=f_h, fill=fill)
    y = 110
    y = _draw_lines(draw, [
        "发票代码：4400204130",
        "发票号码：08876543",
        "开票日期：2026-08-03",
        "销售方：佛山市某化工有限公司",
        "购买方：广东广康化工实业有限公司",
        "金额(不含税)：76500.00",
        "税额：9945.00",
        "价税合计：86445.00",
    ], y=y, font=f, fill=fill, lh=42)
    y += 10
    _draw_table(draw, ["货物名称", "规格", "数量", "单价", "金额"], [
        ["液碱", "32% 离子膜", "30", "2200", "66000"],
        ["片碱", "99% 25kg", "50", "210", "10500"],
    ], x=60, y=y, font=_font(19, handwritten), fill=fill,
        col_w=[220, 220, 120, 150, 160], row_h=40)


def render_compare(draw, handwritten=False):
    f_h = _font(38, handwritten)
    f = _font(25, handwritten)
    fill = "black"
    draw.text((60, 40), "采购比价单", font=f_h, fill=fill)
    y = 115
    y = _draw_lines(draw, [
        "比价单号：BJ20260805007",
        "比价日期：2026-08-05",
        "比价标的：工业级碳酸氢钠 25kg/袋",
        "规格要求：含量≥99%",
        "推荐供应商：清远新元化工",
        "比价结论：选用最低价且资质合格者",
    ], y=y, font=f, fill=fill, lh=43)
    y += 10
    _draw_table(draw, ["供应商", "报价"], [
        ["清远新元化工", "118"],
        ["肇庆华贸", "123"],
        ["江门盛源", "121"],
    ], x=60, y=y, font=_font(21, handwritten), fill=fill,
        col_w=[360, 200], row_h=42)


def render_report(draw, handwritten=False):
    """生产日报 —— list 样式（键值行 + 多条产品产量）"""
    f_h = _font(40, handwritten)
    f = _font(25, handwritten)
    fill = "black"
    draw.text((60, 40), "生产日报", font=f_h, fill=fill)
    y = 120
    y = _draw_lines(draw, [
        "日期：2026-08-06",
        "车间：氯碱车间",
        "班次：早班",
        "计划总产量：120",
        "实际总产量：118",
        "完成率：98.3%",
        "——产品产量——",
        "液碱(32%)：60 吨",
        "片碱(99%)：38 吨",
        "次氯酸钠：20 吨",
        "异常说明：3#罐离心泵检修 1.5h",
    ], y=y, font=f, fill=fill, lh=44)


def render_quote(draw, handwritten=False):
    """报价单 —— table 样式 + 明细数组（验证数组型字段抽取）"""
    f_h = _font(38, handwritten)
    f = _font(24, handwritten)
    fill = "black"
    draw.text((60, 40), "采购报价单", font=f_h, fill=fill)
    y = 112
    y = _draw_lines(draw, [
        "报价单号：BJ20260808012",
        "报价方：清远新元化工有限公司",
        "报价日期：2026-08-08",
        "有效期：2026-08-15",
        "付款条件：货到验收30天",
    ], y=y, font=f, fill=fill, lh=42)
    y += 10
    _draw_table(draw, ["品名", "规格", "数量", "单价", "金额"], [
        ["碳酸氢钠", "99% 25kg/袋", "200", "118", "23600"],
        ["聚合氯化铝", "28% 30kg/袋", "80", "265", "21200"],
        ["片碱", "99% 25kg", "60", "210", "12600"],
    ], x=60, y=y, font=_font(19, handwritten), fill=fill,
        col_w=[220, 220, 120, 150, 160], row_h=40)


def render_contract(draw, handwritten=False):
    """采购合同 —— card 样式（要素卡片）"""
    f_h = _font(36, handwritten)
    f = _font(25, handwritten)
    fill = "black"
    draw.text((60, 40), "采购合同", font=f_h, fill=fill)
    y = 110
    y = _draw_lines(draw, [
        "合同编号：HT20260810009",
        "甲方（采购方）：广东广康化工实业有限公司",
        "乙方（供应方）：清远新元化工有限公司",
        "签订日期：2026-08-10",
        "合同金额：57400.00",
        "付款方式：分期付款",
        "交付周期：15 天",
        "标的：工业级碳酸氢钠等化工原料",
    ], y=y, font=f, fill=fill, lh=44)


DOCS = {
    "delivery": render_delivery,
    "invoice": render_invoice,
    "compare": render_compare,
    "report": render_report,
    "quote": render_quote,
    "contract": render_contract,
}

VARIANTS = ["clear", "blur", "lowcontrast", "rotate", "handwritten"]
FORMATS = ["png", "jpg", "pdf"]


def _apply_variant(img, variant):
    if variant == "blur":
        return img.filter(ImageFilter.GaussianBlur(radius=2.2))
    if variant == "lowcontrast":
        # 灰字 + 浅背景，降低对比
        gray = img.convert("L")
        gray = gray.point(lambda p: int(p * 0.55 + 120))  # 压缩动态范围
        return gray.convert("RGB")
    if variant == "rotate":
        return img.rotate(90, expand=True, fillcolor="white")
    return img  # clear / handwritten


def generate():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    count = 0
    for doc_name, render_fn in DOCS.items():
        doc_dir = OUT_ROOT / doc_name
        doc_dir.mkdir(parents=True, exist_ok=True)
        for variant in VARIANTS:
            handwritten = (variant == "handwritten")
            img, draw = _new_canvas()
            render_fn(draw, handwritten=handwritten)
            img = _apply_variant(img, variant)
            for fmt in FORMATS:
                out = doc_dir / f"{variant}.{fmt}"
                if fmt == "pdf":
                    # 用 PIL 直接写 PDF（每页一张图）
                    img.save(str(out), "PDF", resolution=120.0)
                else:
                    img.save(out, "JPEG" if fmt == "jpg" else fmt.upper())
                count += 1
                print(f"  生成 {doc_name}/{variant}.{fmt}")
    print(f"\n完成：共生成 {count} 张样例图 → {OUT_ROOT}")


if __name__ == "__main__":
    generate()
