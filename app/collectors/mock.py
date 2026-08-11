"""
app/collectors/mock.py — 模拟采集器（开发 / 演示 / 回归测试）

价值：会话内容存档需要企业采购 + 认证 + 成员授权，周期长。
本采集器让 OCR、结构化抽取、入库、前端展示等 90% 的工作可以先行验证，
等存档到位后把 COLLECTOR_MODE 从 mock 改成 archive 即可，其余代码一行不动。

它不是"返回假 JSON"这么简单——会用 PIL 真实绘制中文业务单据图片，
所以 OCR 引擎跑的是真图、抽取模型读的是真识别结果，全链路可信。
"""
from __future__ import annotations

import hashlib
import logging
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from app.collectors.base import BaseCollector, MediaRef, NormalizedMessage
from app.config import settings

logger = logging.getLogger(__name__)

_FIXTURE_DIR = Path(settings.MEDIA_ROOT).parent / "fixtures"

# Windows 常见中文字体，按优先级探测
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _find_font(size: int):
    from PIL import ImageFont

    for fp in _FONT_CANDIDATES:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except OSError:
                continue
    logger.warning("未找到中文字体，OCR 演示效果会受影响")
    return ImageFont.load_default()


# ---------------------------------------------------------------- 造图
def _draw_delivery_note(path: Path, no: str, date: str, rows: list[tuple]) -> None:
    """绘制一张送货单"""
    from PIL import Image, ImageDraw

    W, H = 900, 620
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_title = _find_font(34)
    f_norm = _find_font(21)
    f_small = _find_font(18)

    d.text((300, 28), "送 货 单", font=f_title, fill="black")
    d.text((60, 95), f"送货单号：{no}", font=f_norm, fill="black")
    d.text((520, 95), f"日期：{date}", font=f_norm, fill="black")
    d.text((60, 130), "供应商：广康生化科技股份有限公司", font=f_norm, fill="black")
    d.text((60, 165), "收货单位：晟康生产基地", font=f_norm, fill="black")

    top, row_h = 205, 46
    cols = [60, 130, 400, 500, 620, 760, 850]
    headers = ["序号", "物料名称", "规格", "数量", "单价(元)", "金额(元)"]

    for i in range(len(rows) + 2):
        y = top + i * row_h
        d.line([(cols[0], y), (cols[-1], y)], fill="black", width=1)
    for x in cols:
        d.line([(x, top), (x, top + (len(rows) + 1) * row_h)], fill="black", width=1)

    for i, h in enumerate(headers):
        d.text((cols[i] + 8, top + 12), h, font=f_small, fill="black")

    total = 0.0
    for r, row in enumerate(rows, start=1):
        y = top + r * row_h + 12
        name, spec, qty, price = row
        amount = qty * price
        total += amount
        for i, v in enumerate([str(r), name, spec, str(qty), f"{price:.2f}", f"{amount:.2f}"]):
            d.text((cols[i] + 8, y), v, font=f_small, fill="black")

    y_total = top + (len(rows) + 1) * row_h + 20
    d.text((cols[0], y_total), f"合计金额：￥{total:.2f} 元", font=f_norm, fill="black")
    d.text((cols[0], y_total + 40), "送货人：张伟          收货人：李明", font=f_small, fill="black")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=95)


def _draw_daily_report(path: Path, date: str, items: list[tuple]) -> None:
    """绘制一张生产日报"""
    from PIL import Image, ImageDraw

    W, H = 860, 560
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_title = _find_font(32)
    f_norm = _find_font(21)
    f_small = _find_font(19)

    d.text((280, 26), "生产日报表", font=f_title, fill="black")
    d.text((60, 88), f"报表日期：{date}", font=f_norm, fill="black")
    d.text((520, 88), "车间：一车间", font=f_norm, fill="black")

    top, row_h = 140, 48
    cols = [60, 260, 420, 570, 800]
    headers = ["产品名称", "计划产量(吨)", "实际产量(吨)", "完成率"]

    for i in range(len(items) + 2):
        y = top + i * row_h
        d.line([(cols[0], y), (cols[-1], y)], fill="black", width=1)
    for x in cols:
        d.line([(x, top), (x, top + (len(items) + 1) * row_h)], fill="black", width=1)
    for i, h in enumerate(headers):
        d.text((cols[i] + 10, top + 14), h, font=f_small, fill="black")

    for r, (name, plan, actual) in enumerate(items, start=1):
        y = top + r * row_h + 14
        rate = f"{actual / plan * 100:.1f}%" if plan else "-"
        for i, v in enumerate([name, f"{plan:.1f}", f"{actual:.1f}", rate]):
            d.text((cols[i] + 10, y), v, font=f_small, fill="black")

    y_end = top + (len(items) + 1) * row_h + 24
    d.text((60, y_end), "记录人：王芳", font=f_small, fill="black")
    d.text((60, y_end + 34), "备注：设备运行正常，无异常停机。", font=f_small, fill="black")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=95)


def _ensure_fixtures() -> list[dict]:
    """生成（或复用）演示图片，返回元信息"""
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now()

    specs = [
        {
            "file": _FIXTURE_DIR / "delivery_note_001.jpg",
            "kind": "delivery",
            "name": "送货单-20260804-001.jpg",
            "build": lambda p: _draw_delivery_note(
                p,
                no="SH20260804001",
                date=today.strftime("%Y-%m-%d"),
                rows=[
                    ("草甘膦原药", "95%TC", 12, 26800.00),
                    ("助剂A", "工业级", 30, 1450.50),
                    ("包装桶", "200L", 60, 185.00),
                ],
            ),
        },
        {
            "file": _FIXTURE_DIR / "daily_report_001.jpg",
            "kind": "report",
            "name": "生产日报-20260804.jpg",
            "build": lambda p: _draw_daily_report(
                p,
                date=today.strftime("%Y-%m-%d"),
                items=[
                    ("草甘膦原药", 50.0, 48.6),
                    ("双甘膦", 30.0, 31.2),
                    ("亚磷酸二甲酯", 20.0, 18.4),
                ],
            ),
        },
        {
            "file": _FIXTURE_DIR / "delivery_note_002.jpg",
            "kind": "delivery",
            "name": "送货单-20260804-002.jpg",
            "build": lambda p: _draw_delivery_note(
                p,
                no="SH20260804002",
                date=(today - timedelta(days=1)).strftime("%Y-%m-%d"),
                rows=[
                    ("液碱", "32%", 25, 980.00),
                    ("盐酸", "31%", 18, 520.00),
                ],
            ),
        },
    ]

    out = []
    for s in specs:
        p: Path = s["file"]
        if not p.exists():
            try:
                s["build"](p)
                logger.info("已生成演示图片 %s", p.name)
            except Exception as e:  # noqa: BLE001
                logger.error("生成演示图片失败 %s：%s", p.name, e)
                continue
        blob = p.read_bytes()
        out.append(
            {
                "path": p,
                "kind": s["kind"],
                "name": s["name"],
                "size": len(blob),
                "md5": hashlib.md5(blob).hexdigest(),
            }
        )
    return out


class MockCollector(BaseCollector):
    """
    模拟"群里陆续有人发消息"。

    每次 fetch 返回一小批新消息，seq 单调递增，行为与真实存档一致
    （包括：只返回 seq 更大的消息、有文本有图片、混杂无关闲聊）。
    """

    name = "mock"

    # 风险话术样本（演示双引擎检测用）。
    # 注意：采集器每 3 条插 1 张图（seq % 3 == 0 为图片，图片走 OCR 文本），
    # 所以这里把风险样本都放在"文本位"（索引 1,2,4,5,7,8,10,11），
    # 避免被图片消息吞掉导致关键词检测不到。每条样本命中一个明确分类。
    _CHATTER = [
        "今天的货已经安排发出了",                            # idx0  seq12 图片(非风险)
        "收到，我这边核对一下数量",                          # idx1  seq1  文本(非风险)
        "这个价格明显低于市场价了，你核一下",                # idx2  seq2  价格异常
        "下午三点前需要回传签收单",                          # idx3  seq3  图片(非风险)
        "这笔单咱们私下处理，别入库，绕开公司就行",          # idx4  seq4  私下交易/飞单
        "给你个点回扣，这单你帮忙签了",                      # idx5  seq5  回扣/利益输送
        "仓库那边说库位不够了，先堆到二号库",                # idx6  seq6  图片(非风险)
        "他要打12315投诉，说产品质量有问题要赔偿",          # idx7  seq7  客户投诉/舆情
        "把咱们底价发给外面那家，别外泄啊",                  # idx8  seq8  信息泄露
        "本月产量目标应该能达成",                            # idx9  seq9  图片(非风险)
        "客户说想看看竞品A的报价，问我们能不能更便宜",      # idx10 seq10 竞品撬单
        "用假发票入账就行，别让税务查到",                    # idx11 seq11 合规风险
    ]

    _ROOMS = [
        ("wrOgQhDgAAv0k1234567890abcdefg", "生产运营群"),
        ("wrOgQhDgAAv0k0987654321zyxwvu", "供应链协同群"),
        ("wrOgQhDgAAcust0abcdefghijklmn", "客户沟通群"),
        ("wrTimeoutDemo001", "客户售后群(超时演示)"),
    ]

    # 固定 seq → 群 映射，让三类高风险场景（价格/私下交易/回扣/投诉/信息泄露/竞品/合规）
    # 稳定分布在三个群里（验证"不同群 → 不同管理层"路由）。
    _ROOM_BY_SEQ = {
        1: "wrOgQhDgAAv0k1234567890abcdefg",   # 生产运营群
        2: "wrOgQhDgAAcust0abcdefghijklmn",     # 客户沟通群
        3: "wrOgQhDgAAv0k0987654321zyxwvu",     # 供应链协同群
        4: "wrOgQhDgAAv0k1234567890abcdefg",   # 生产运营群
        5: "wrOgQhDgAAcust0abcdefghijklmn",     # 客户沟通群
        6: "wrOgQhDgAAv0k0987654321zyxwvu",     # 供应链协同群
        7: "wrOgQhDgAAv0k1234567890abcdefg",   # 生产运营群
        8: "wrOgQhDgAAcust0abcdefghijklmn",     # 客户沟通群
        9: "wrOgQhDgAAv0k0987654321zyxwvu",     # 供应链协同群
        10: "wrOgQhDgAAcust0abcdefghijklmn",    # 客户沟通群
        11: "wrOgQhDgAAv0k0987654321zyxwvu",     # 供应链协同群
        12: "wrOgQhDgAAv0k1234567890abcdefg",   # 生产运营群
        # 超时演示：员工一句问候(久) → 客户连发提问且无人回复（验证"服务响应超时"）
        13: "wrTimeoutDemo001",
        14: "wrTimeoutDemo001",
        15: "wrTimeoutDemo001",
    }

    # 超时演示消息：(from_id, 正文, 距今分钟)。from_id 以 wo 开头=外部客户(需回复)；
    # user_ 开头=企业员工(客服)。员工消息时间久远(曾问候)，客户消息距今较久且其后无回复。
    _TIMEOUT_MSGS = [
        ("user_kefu", "您好，这里是售后客服，请问有什么可以帮您？", 180),
        ("woCustTimeout001", "你好，我的订单什么时候能发货？已经等了好久了", 50),
        ("woCustTimeout001", "在吗？很急，麻烦尽快回复一下，谢谢", 48),
    ]

    def __init__(self):
        self._fixtures = _ensure_fixtures()
        self._rng = random.Random(20260804)  # 固定种子，结果可复现

    def health_check(self) -> tuple[bool, str]:
        if not self._fixtures:
            return False, "演示图片生成失败（缺少 Pillow 或中文字体）"
        return True, f"Mock 模式就绪，{len(self._fixtures)} 个演示附件"

    # ------------------------------------------------------------------
    def fetch(self, seq: int, limit: int) -> list[NormalizedMessage]:
        # 演示数据总量有限：seq 超过 12 后不再产出，避免无限造数据撑爆库。
        # 注意：返回量必须尊重调用方传入的 limit（page size），不能自行再设更小上限，
        # 否则 sync_messages 的"不足一批即追上"判定会误判提前退出。仅当 15-seq<=limit
        # 时本批才不满，此时 seq 已耗尽，确实追上了。
        if seq >= 15:
            return []

        msgs: list[NormalizedMessage] = []
        now_ms = int(datetime.now().timestamp() * 1000)
        n = min(limit, 15 - seq)

        for i in range(n):
            cur = seq + i + 1
            # 超时演示消息（seq 13-15）：直接构造，绕过普通 _CHATTER 逻辑
            if cur >= 13 and cur - 13 < len(self._TIMEOUT_MSGS):
                frm, text, age = self._TIMEOUT_MSGS[cur - 13]
                msgs.append(
                    NormalizedMessage(
                        seq=cur,
                        msgid=f"mock_to_{cur}",
                        msg_type="text",
                        from_id=frm,
                        room_id=self._ROOM_BY_SEQ.get(cur, "wrTimeoutDemo001"),
                        msg_time_ms=now_ms - age * 60_000,
                        content_text=text,
                        raw={"msgtype": "text", "_mock": True},
                    )
                )
                continue

            room_id = self._ROOM_BY_SEQ.get(cur, self._ROOMS[cur % len(self._ROOMS)][0])
            sender = f"user_{['zhangwei', 'liming', 'wangfang'][cur % 3]}"

            # 每 3 条里插 1 条带附件的消息
            if cur % 3 == 0 and self._fixtures:
                fx = self._fixtures[(cur // 3 - 1) % len(self._fixtures)]
                msgs.append(
                    NormalizedMessage(
                        seq=cur,
                        msgid=f"mock_{cur}_{fx['md5'][:8]}",
                        msg_type="image",
                        from_id=sender,
                        room_id=room_id,
                        msg_time_ms=now_ms - (12 - cur) * 60_000,
                        content_text=f"[image] {fx['name']}",
                        raw={"msgtype": "image", "_mock": True},
                        medias=[
                            MediaRef(
                                media_type="image",
                                sdkfileid=f"mock_sdkfileid_{fx['md5']}",
                                file_name=fx["name"],
                                file_ext=".jpg",
                                file_size=fx["size"],
                                md5sum=fx["md5"],
                                local_path=str(fx["path"]),
                            )
                        ],
                    )
                )
            else:
                msgs.append(
                    NormalizedMessage(
                        seq=cur,
                        msgid=f"mock_{cur}",
                        msg_type="text",
                        from_id=sender,
                        room_id=room_id,
                        msg_time_ms=now_ms - (12 - cur) * 60_000,
                        content_text=self._CHATTER[cur % len(self._CHATTER)],
                        raw={"msgtype": "text", "_mock": True},
                    )
                )

        return msgs

    # ------------------------------------------------------------------
    def download_media(self, media: MediaRef, dest_path: str) -> int:
        """mock 模式直接从 fixtures 拷贝，模拟下载过程"""
        if not media.local_path or not Path(media.local_path).exists():
            raise ValueError(f"演示附件不存在：{media.local_path}")

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(media.local_path, dest)
        return dest.stat().st_size
