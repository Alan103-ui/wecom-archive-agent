"""
app/services/risk/categories.py — 风险分类枚举、关键词库与默认种子数据

默认种子直接对齐用户场景：**客户沟通群 + 采购沟通群**，并对不同群路由到不同管理层。

设计：
    - 分类是固定枚举（不进库），规则引用分类字符串，页面可增删规则但不动枚举。
    - 默认规则按"采购/客户"两类高频风险预设，scope_rooms 留空=全群生效，
      真实部署时在「风控配置」页把 scope_rooms 改成具体 roomid 即可按群隔离。
    - 管理层 L1 业务主管 / L2 部门总监 / L3 总经理·合规；severity 兜底映射见
      DEFAULT_SEVERITY_LAYERS。
"""
from __future__ import annotations

# ---------------- 固定风险分类 ----------------
CATEGORY_PRICE = "价格异常"
CATEGORY_SIDE_DEAL = "私下交易/飞单"
CATEGORY_KICKBACK = "回扣/利益输送"
CATEGORY_COMPETITOR = "竞品撬单"
CATEGORY_COMPLAINT = "客户投诉/舆情"
CATEGORY_INFO_LEAK = "信息泄露"
CATEGORY_COMPLIANCE = "合规风险"

ALL_CATEGORIES = [
    CATEGORY_PRICE,
    CATEGORY_SIDE_DEAL,
    CATEGORY_KICKBACK,
    CATEGORY_COMPETITOR,
    CATEGORY_COMPLAINT,
    CATEGORY_INFO_LEAK,
    CATEGORY_COMPLIANCE,
]

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITY_CRITICAL = "critical"

SEVERITY_ORDER = {SEVERITY_LOW: 1, SEVERITY_MEDIUM: 2, SEVERITY_HIGH: 3, SEVERITY_CRITICAL: 4}

# 严重度 → 兜底管理层（规则未显式指定 alert_layers 时用）
# 严重度越高，触达的层级越多（基层先知道，高层必知会）
DEFAULT_SEVERITY_LAYERS = {
    SEVERITY_LOW: ["L1"],
    SEVERITY_MEDIUM: ["L1", "L2"],
    SEVERITY_HIGH: ["L2", "L3"],
    SEVERITY_CRITICAL: ["L1", "L2", "L3"],
}

# ---------------- 关键词库（正则，命中任一即触发；按分类组织，便于默认规则引用） ----------------
KEYWORD_LIB: dict[str, list[str]] = {
    CATEGORY_PRICE: [
        r"不走账", r"不开发票", r"私账", r"账外", r"低于?市场价", r"低于?合同价",
        r"价格.?差", r"飞单.?价", r"回扣.?价",
    ],
    CATEGORY_SIDE_DEAL: [
        r"私下", r"飞单", r"体外循环", r"走?别的?渠道", r"绕过?公司", r"私下转",
        r"别?让?公司?知道", r"不上系统", r"不入库",
    ],
    CATEGORY_KICKBACK: [
        r"回扣", r"返点", r"好处费", r"提成?私下", r"辛苦费", r"介绍费",
        r"返利.?私下", r"给.?点好处", r"茶水费",
    ],
    CATEGORY_COMPETITOR: [
        r"竞品", r"竞争对手", r"友商", r"别家", r"另一家", r"比?他们?便宜",
        r"撬?单", r"挖?客户", r"转?去?.*?公司",
    ],
    CATEGORY_COMPLAINT: [
        r"投诉", r"要?退货", r"赔偿", r"曝光", r"找?媒体", r"找?监管",
        r"太差", r"欺诈", r"欺骗", r"黑猫", r"12315", r"舆情",
    ],
    CATEGORY_INFO_LEAK: [
        r"内部?价", r"底价", r"成本价", r"泄露", r"外传", r"发给?外面",
        r"别?外泄", r"保密.?价", r"客户?名单", r"名单?给",
    ],
    CATEGORY_COMPLIANCE: [
        r"无证", r"资质?不全", r"违规", r"避税", r"走私", r"假?发票",
        r"阴阳?合同", r"账?外?账",
    ],
}

# ---------------- 默认管理层 ----------------
DEFAULT_LAYERS = [
    {"id": "L1", "name": "业务主管层", "level": 1, "description": "一线主管，先触达、先处置"},
    {"id": "L2", "name": "部门总监层", "level": 2, "description": "采购/销售/客服负责人"},
    {"id": "L3", "name": "总经理·合规层", "level": 3, "description": "高管与合规，重大风险必知会"},
]

# ---------------- 默认投递目标（占位，用户换成真实地址后启用） ----------------
# 逐层各挂一个 system 目标（系统内风险页红点），这样"事件路由到了哪一层"在投递回执里清晰可见；
# webhook/app/email 为外部通道，默认关闭或占位，用户配置真实地址后启用即可生效。
DEFAULT_TARGETS = [
    {"layer_id": "L1", "channel": "system", "target": "in-app", "label": "系统内通知(L1)", "enabled": True},
    {"layer_id": "L2", "channel": "system", "target": "in-app", "label": "系统内通知(L2)", "enabled": True},
    {"layer_id": "L3", "channel": "system", "target": "in-app", "label": "系统内通知(L3)", "enabled": True},
    # 演示用占位 webhook：真实部署替换 key 后启用；此处保持关闭，避免无谓请求
    {"layer_id": "L3", "channel": "webhook",
     "target": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_WITH_REAL",
     "label": "合规告警群(企微机器人)", "enabled": False},
]

# ---------------- 默认风险规则（采购 + 客户高频场景） ----------------
# scope_rooms 留空=全群生效；真实部署在页面按 roomid 隔离即可实现"不同群→不同层"。
DEFAULT_RULES = [
    {
        "name": "采购-私下交易/飞单", "category": CATEGORY_SIDE_DEAL, "severity": SEVERITY_HIGH,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_SIDE_DEAL],
        "alert_layers": ["L2", "L3"],
        "description": "采购沟通中若出现绕过公司、体外循环、飞单等话术",
    },
    {
        "name": "采购-回扣/利益输送", "category": CATEGORY_KICKBACK, "severity": SEVERITY_CRITICAL,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_KICKBACK],
        "alert_layers": ["L3"],
        "description": "任何涉及回扣、好处费、返点的表述，直接上报合规",
    },
    {
        "name": "采购-价格异常", "category": CATEGORY_PRICE, "severity": SEVERITY_HIGH,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_PRICE],
        "alert_layers": ["L2"],
        "description": "不走公账、不开票、明显偏离合同/市场价的议价",
    },
    {
        "name": "客户-竞品撬单", "category": CATEGORY_COMPETITOR, "severity": SEVERITY_MEDIUM,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_COMPETITOR],
        "alert_layers": ["L2"],
        "description": "客户群里出现竞品对比、被撬单、转投他司等信号",
    },
    {
        "name": "客户-投诉/舆情", "category": CATEGORY_COMPLAINT, "severity": SEVERITY_MEDIUM,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_COMPLAINT],
        "alert_layers": ["L1", "L2"],
        "description": "客户表达投诉、退货、赔偿、曝光、找监管等不满",
    },
    {
        "name": "通用-信息泄露", "category": CATEGORY_INFO_LEAK, "severity": SEVERITY_HIGH,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_INFO_LEAK],
        "alert_layers": ["L2", "L3"],
        "description": "底价/成本/客户名单等敏感信息外泄风险",
    },
    {
        "name": "通用-合规风险", "category": CATEGORY_COMPLIANCE, "severity": SEVERITY_HIGH,
        "scope_rooms": [], "keywords": KEYWORD_LIB[CATEGORY_COMPLIANCE],
        "alert_layers": ["L3"],
        "description": "资质不全、假发票、阴阳合同、避税等合规红线",
    },
]
