"""
scripts/gen_sample_images.py — 生成「结构化抽取」多场景压力测试图

覆盖维度：
  · 文档类型：送货单 / 增值税发票 / 采购比价单 / 生产日报(list) / 报价单(table+明细数组) / 采购合同(card)
  · 样式变体：clear(清晰) / blur(模糊) / lowcontrast(低对比) / rotate(旋转90°) / handwritten(仿真手写)
  · 文件格式：png / jpg / pdf

handwritten 变体说明（关键）：
  旧版用楷体直接 draw.text（本质是打印体，RapidOCR 置信度仍 0.99，触发不了视觉兜底）。
  新版改为【逐字随机旋转+垂直抖动+笔迹粗细扰动+纸张噪点】的仿真手写，
  使 RapidOCR 置信度真正下降（通常 < 0.85），从而能验证「低置信→视觉抽取兜底」链路。

输出目录：data/sample_images/<doc>/<variant>.<ext>
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont, ImageFilter  # noqa: E402

OUT_ROOT = ROOT / "data" / "sample_images"

HEI = "C:/Windows/Fonts/simhei.ttf"
KAI = "C:/Windows/Fonts/simkai.ttf"


def _font(size, handwritten=False):
    path = KAI if handwritten else HEI
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _new_canvas(w=1000, h=1400):
    # 用 RGBA，便于逐字旋转合成
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    return img, ImageDraw.Draw(img)


def _put_text(draw, text, x, y, font, fill, handwritten, rng):
    """绘制一段文字。handwritten 时逐字随机旋转/偏移，模拟手写抖动。"""
    if not handwritten:
        draw.text((x, y), text, font=font, fill=fill)
        return x + int(draw.textlength(text, font=font))
    # 手写：逐字渲染到临时 RGBA，旋转后合成到画布
    cx = x
    for ch in text:
        if ch == " ":
            cx += int(draw.textlength("  ", font=font) / 2)
            continue
        gsz = int(font.size * 1.6)
        tmp = Image.new("RGBA", (gsz, gsz), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        td.text((gsz // 2, gsz // 2), ch, font=font, fill=fill)
        ang = rng.uniform(-13, 13)
        dy = rng.uniform(-7, 7)
        t2 = tmp.rotate(ang, expand=True, fillcolor=(0, 0, 0, 0))
        # 居中贴合
        ox = cx - t2.width // 2
        oy = int(y + font.size * 0.2 + dy) - gsz // 2
        draw._image.alpha_composite(t2, (ox, oy))
        cx += int(draw.textlength(ch, font=font) * rng.uniform(0.85, 1.2))
    return cx


def _draw_lines(draw, lines, x=60, y=60, font=None, fill="black", lh=46, handwritten=False, rng=None):
    f = font or _font(28)
    for ln in lines:
        _put_text(draw, ln, x, y, f, fill, handwritten, rng)
        y += lh
    return y


def _draw_table(draw, headers, rows, x=60, y=60, font=None, fill="black",
                col_w=None, row_h=44, lh=34, handwritten=False, rng=None):
    f = font or _font(22)
    cw = col_w or [180] * len(headers)
    cx = x
    for h, w_ in zip(headers, cw):
        _put_text(draw, str(h), cx, y, f, fill, handwritten, rng)
        cx += w_
    y += row_h
    draw.line([(x, y - 8), (x + sum(cw), y - 8)], fill=fill, width=1)
    for r in rows:
        cx = x
        for val, w_ in zip(r, cw):
            _put_text(draw, str(val), cx, y, f, fill, handwritten, rng)
            cx += w_
        y += row_h
    return y


def _paper_noise(img, rng):
    """轻微纸张噪点 + 横线格，增强手写真实感。返回 RGB。"""
    rgb = img.convert("RGB")
    px = rgb.load()
    w, h = rgb.size
    for _ in range(int(w * h * 0.004)):
        x = rng.randint(0, w - 1)
        y = rng.randint(0, h - 1)
        r, g, b = px[x, y]
        d = rng.randint(-14, 14)
        px[x, y] = (max(0, min(255, r + d)), max(0, min(255, g + d)), max(0, min(255, b + d)))
    # 淡横线格
    draw = ImageDraw.Draw(rgb)
    for yy in range(120, h, 46):
        draw.line([(0, yy), (w, yy)], fill=(225, 225, 225), width=1)
    return rgb.filter(ImageFilter.GaussianBlur(radius=1.6))  # 墨晕+笔画粘连，降低 OCR 可读性


# ---------------------------------------------------------------- 六类单据内容
def _render_doc(name, title, head_lines, table=None, handwritten=False, rng=None):
    img, draw = _new_canvas()
    fill = (25, 25, 25, 255) if handwritten else (0, 0, 0, 255)
    f_h = _font(40, handwritten)
    f = _font(26, handwritten)
    _put_text(draw, title, 60, 40, f_h, fill, handwritten, rng)
    y = 120
    y = _draw_lines(draw, head_lines, y=y, font=f, fill=fill, lh=44, handwritten=handwritten, rng=rng)
    y += 10
    if table:
        headers, rows, col_w = table
        _draw_table(draw, headers, rows, x=60, y=y,
                    font=_font(20, handwritten), fill=fill,
                    col_w=col_w, row_h=42, handwritten=handwritten, rng=rng)
    if handwritten:
        img = _paper_noise(img, rng)
    else:
        img = img.convert("RGB")
    return img


def render_delivery(handwritten=False, rng=None):
    return _render_doc(
        "delivery", "送货单",
        ["送货单号：SH20260804001", "送货日期：2026-08-04",
         "供应商：广东广康化工实业有限公司", "收货单位：广州材料仓库", "合计金额：86500"],
        (["物料名称", "规格", "数量", "单价", "金额"],
         [["碳酸氢钠", "工业一级 25kg/袋", "500", "120", "60000"],
          ["聚合氯化铝", "28% 30kg/袋", "100", "265", "26500"]],
         [240, 260, 120, 140, 160]),
        handwritten, rng,
    )


def render_invoice(handwritten=False, rng=None):
    return _render_doc(
        "invoice", "增值税专用发票",
        ["发票代码：4400204130", "发票号码：08876543", "开票日期：2026-08-03",
         "销售方：佛山市某化工有限公司", "购买方：广东广康化工实业有限公司",
         "金额(不含税)：76500.00", "税额：9945.00", "价税合计：86445.00"],
        (["货物名称", "规格", "数量", "单价", "金额"],
         [["液碱", "32% 离子膜", "30", "2200", "66000"],
          ["片碱", "99% 25kg", "50", "210", "10500"]],
         [220, 220, 120, 150, 160]),
        handwritten, rng,
    )


def render_compare(handwritten=False, rng=None):
    return _render_doc(
        "compare", "采购比价单",
        ["比价单号：BJ20260805007", "比价日期：2026-08-05",
         "比价标的：工业级碳酸氢钠 25kg/袋", "规格要求：含量≥99%",
         "推荐供应商：清远新元化工", "比价结论：选用最低价且资质合格者"],
        (["供应商", "报价"],
         [["清远新元化工", "118"], ["肇庆华贸", "123"], ["江门盛源", "121"]],
         [360, 200]),
        handwritten, rng,
    )


def render_report(handwritten=False, rng=None):
    return _render_doc(
        "report", "生产日报",
        ["日期：2026-08-06", "车间：氯碱车间", "班次：早班", "计划总产量：120",
         "实际总产量：118", "完成率：98.3%", "——产品产量——",
         "液碱(32%)：60 吨", "片碱(99%)：38 吨", "次氯酸钠：20 吨",
         "异常说明：3#罐离心泵检修 1.5h"],
        None, handwritten, rng,
    )


def render_quote(handwritten=False, rng=None):
    return _render_doc(
        "quote", "采购报价单",
        ["报价单号：BJ20260808012", "报价方：清远新元化工有限公司", "报价日期：2026-08-08",
         "有效期：2026-08-15", "付款条件：货到验收30天"],
        (["品名", "规格", "数量", "单价", "金额"],
         [["碳酸氢钠", "99% 25kg/袋", "200", "118", "23600"],
          ["聚合氯化铝", "28% 30kg/袋", "80", "265", "21200"],
          ["片碱", "99% 25kg", "60", "210", "12600"]],
         [220, 220, 120, 150, 160]),
        handwritten, rng,
    )


def render_contract(handwritten=False, rng=None):
    return _render_doc(
        "contract", "采购合同",
        ["合同编号：HT20260810009", "甲方（采购方）：广东广康化工实业有限公司",
         "乙方（供应方）：清远新元化工有限公司", "签订日期：2026-08-10",
         "合同金额：57400.00", "付款方式：分期付款", "交付周期：15 天",
         "标的：工业级碳酸氢钠等化工原料"],
        None, handwritten, rng,
    )


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
        gray = img.convert("L")
        gray = gray.point(lambda p: int(p * 0.55 + 120))
        return gray.convert("RGB")
    if variant == "rotate":
        return img.rotate(90, expand=True, fillcolor="white")
    return img  # clear / handwritten（handwritten 已在渲染时加噪）


def generate():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    count = 0
    for doc_name, render_fn in DOCS.items():
        doc_dir = OUT_ROOT / doc_name
        doc_dir.mkdir(parents=True, exist_ok=True)
        for variant in VARIANTS:
            rng = random.Random(hash((doc_name, variant)) & 0xFFFFFFFF)
            handwritten = (variant == "handwritten")
            img = render_fn(handwritten=handwritten, rng=rng)
            img = _apply_variant(img, variant)
            for fmt in FORMATS:
                out = doc_dir / f"{variant}.{fmt}"
                if fmt == "pdf":
                    img.save(str(out), "PDF", resolution=120.0)
                else:
                    img.save(out, "JPEG" if fmt == "jpg" else fmt.upper())
                count += 1
                print(f"  生成 {doc_name}/{variant}.{fmt}")
    print(f"\n完成：共生成 {count} 张样例图 → {OUT_ROOT}")


if __name__ == "__main__":
    generate()
