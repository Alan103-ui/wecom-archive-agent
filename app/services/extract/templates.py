"""
app/services/extract/templates.py — 抽取模板的种子数据与匹配逻辑

设计意图：
业务字段随时会变（今天抽送货单，明天要抽质检报告），
如果把字段写死在代码里，每次变更都要改代码 + 重启。
所以把「抽什么字段」下沉成数据库里的模板记录，可在管理页增删改。

匹配算法（简单可解释，避免玄学）：
    1. 文件扩展名不在白名单 → 淘汰
    2. 统计 OCR 文本命中了多少个关键词
    3. 命中数 > 0 的模板里，按 (命中数, priority) 取最大
    4. 全部未命中 → 用 is_fallback 兜底模板
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ExtractTemplate

logger = logging.getLogger(__name__)


# 开箱即用的默认模板。首次启动自动播种，用户可在页面上改。
DEFAULT_TEMPLATES: list[dict] = [
    {
        "name": "送货单",
        "description": "供应商送货单/发货单，抽取单号、日期、供应商与物料明细",
        "priority": 20,
        "display_style": "table",
        "scenario": "仓库收货 / 采购对账",
        "match_keywords": ["送货单", "送货", "发货单", "收货单位", "供应商", "送货人"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "delivery_no", "label": "送货单号", "type": "string", "desc": "如 SH20260804001"},
            {"key": "delivery_date", "label": "送货日期", "type": "date", "desc": "格式 YYYY-MM-DD"},
            {"key": "supplier", "label": "供应商", "type": "string"},
            {"key": "receiver", "label": "收货单位", "type": "string"},
            {"key": "total_amount", "label": "合计金额", "type": "number", "desc": "纯数字，不要带￥和逗号"},
            {"key": "total_qty", "label": "合计数量", "type": "number", "desc": "纯数字"},
            {"key": "contact", "label": "联系人", "type": "string"},
            {"key": "remark", "label": "备注", "type": "string"},
            {
                "key": "items",
                "label": "物料明细",
                "type": "array",
                "desc": "数组，每行物料，含 name/code/spec/unit/qty/unit_price/amount",
                "items": [
                    {"key": "name", "label": "物料名称", "type": "string"},
                    {"key": "code", "label": "物料编码", "type": "string", "desc": "物料/SKU 编码，如 WL001，无则留空"},
                    {"key": "spec", "label": "规格型号", "type": "string"},
                    {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如实提取：件/吨/个/箱/米/公斤等"},
                    {"key": "qty", "label": "数量", "type": "number"},
                    {"key": "unit_price", "label": "单价", "type": "number"},
                    {"key": "amount", "label": "金额", "type": "number"},
                ],
            },
        ],
        "prompt_extra": "金额字段一律输出纯数字。数量与单价相乘应等于金额，若不符以表格原值为准。"
                       "单位按表格原标注如实提取（如 件/吨/个/箱/米/公斤），不要换算；"
                       "物料编码如实提取原文中的编码，无则填 null。"
                       "合计数量、联系人、备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "生产日报",
        "description": "车间生产日报表，抽取日期、车间与各产品产量",
        "priority": 20,
        "display_style": "mixed",
        "scenario": "车间生产管理 / 产量考核",
        "match_keywords": ["生产日报", "日报表", "计划产量", "实际产量", "完成率", "车间"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "report_date", "label": "报表日期", "type": "date", "desc": "格式 YYYY-MM-DD"},
            {"key": "workshop", "label": "车间", "type": "string"},
            {"key": "recorder", "label": "记录人", "type": "string"},
            {
                "key": "products",
                "label": "产品产量",
                "type": "array",
                "desc": "数组，每行是一个产品的产量记录",
                "items": [
                    {"key": "code", "label": "产品编码", "type": "string", "desc": "产品/SKU 编码，无则留空"},
                    {"key": "name", "label": "产品名称", "type": "string"},
                    {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如 吨/件/台"},
                    {"key": "plan_qty", "label": "计划产量", "type": "number"},
                    {"key": "actual_qty", "label": "实际产量", "type": "number"},
                    {"key": "rate", "label": "完成率", "type": "string", "desc": "保留原文百分号形式，如 98%"},
                ],
            },
            {"key": "remark", "label": "备注", "type": "string"},
        ],
        "prompt_extra": "产量为数字，单位统一按表中标注（通常为吨）。完成率保留百分号形式的原文。",
        "is_fallback": False,
    },
    {
        "name": "增值税发票",
        "description": "增值税专用/普通发票",
        "priority": 30,
        "display_style": "card",
        "scenario": "财务报销 / 税务核算",
        "match_keywords": ["发票", "增值税", "纳税人识别号", "价税合计", "开票日期", "销售方"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "invoice_code", "label": "发票代码", "type": "string"},
            {"key": "invoice_no", "label": "发票号码", "type": "string"},
            {"key": "invoice_date", "label": "开票日期", "type": "date"},
            {"key": "seller_name", "label": "销售方名称", "type": "string"},
            {"key": "seller_tax_no", "label": "销售方纳税人识别号", "type": "string"},
            {"key": "buyer_name", "label": "购买方名称", "type": "string"},
            {"key": "amount", "label": "金额(不含税)", "type": "number"},
            {"key": "tax_amount", "label": "税额", "type": "number"},
            {"key": "total_amount", "label": "价税合计", "type": "number"},
            {"key": "tax_rate", "label": "税率", "type": "string", "desc": "如 13% / 9% / 3%，无则留空"},
            {"key": "currency", "label": "币种", "type": "string", "desc": "默认 人民币，跨境填 USD/EUR 等"},
            {"key": "drawer", "label": "开票人", "type": "string"},
            {"key": "seller_address", "label": "销售方地址及电话", "type": "string"},
            {"key": "buyer_address", "label": "购买方地址", "type": "string"},
            {"key": "remark", "label": "备注", "type": "string"},
            {
                "key": "items",
                "label": "货物明细",
                "type": "array",
                "desc": "数组，每行货物，含 name/code/spec/unit/qty/unit_price/amount",
                "items": [
                    {"key": "name", "label": "货物名称", "type": "string"},
                    {"key": "code", "label": "货物编码", "type": "string", "desc": "货物/SKU 编码，无则留空"},
                    {"key": "spec", "label": "规格型号", "type": "string"},
                    {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如实提取：件/吨/个/箱/米等"},
                    {"key": "qty", "label": "数量", "type": "number"},
                    {"key": "unit_price", "label": "单价", "type": "number"},
                    {"key": "amount", "label": "金额", "type": "number"},
                ],
            },
        ],
        "prompt_extra": "发票号码通常为 8 位或 20 位数字。金额输出纯数字。"
                       "单位与货物编码按原文如实提取，无编码则填 null。"
                       "税率按原文如 13%；币种默认人民币；开票人、备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "采购寻源",
        "description": "采购寻源/采购需求单，抽取寻源单号、品类、预算、寻源方式与候选供应商",
        "priority": 18,
        "display_style": "card",
        "scenario": "采购计划 / 供应商开发",
        "match_keywords": ["采购寻源", "寻源", "采购需求", "招标", "询价采购", "竞价", "候选供应商", "寻源方式"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "sourcing_no", "label": "寻源单号", "type": "string", "desc": "如 PR202608001"},
            {"key": "sourcing_date", "label": "寻源日期", "type": "date", "desc": "格式 YYYY-MM-DD"},
            {"key": "category", "label": "采购品类", "type": "string", "desc": "如 原材料/设备/服务"},
            {"key": "material_name", "label": "物料名称", "type": "string", "desc": "寻源标的物料名称"},
            {"key": "material_code", "label": "物料编码", "type": "string", "desc": "物料/SKU 编码，无则留空"},
            {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如 吨/件/个"},
            {"key": "demand_dept", "label": "需求部门", "type": "string"},
            {"key": "est_qty", "label": "预估数量", "type": "number"},
            {"key": "budget", "label": "预算金额", "type": "number", "desc": "纯数字，不要带￥和逗号"},
            {"key": "method", "label": "寻源方式", "type": "string", "desc": "招标/询价/竞价/单一来源等"},
            {"key": "candidate_suppliers", "label": "候选供应商", "type": "array", "desc": "候选供应商名称数组"},
            {"key": "expected_delivery", "label": "期望交货期", "type": "date"},
            {"key": "owner", "label": "寻源负责人", "type": "string"},
            {"key": "status", "label": "寻源状态", "type": "string", "desc": "如 进行中/已完成/已取消"},
            {"key": "currency", "label": "币种", "type": "string", "desc": "默认 人民币，跨境填 USD/EUR 等"},
            {"key": "remark", "label": "备注", "type": "string"},
        ],
        "prompt_extra": "金额一律输出纯数字。寻源方式从文本中明确表述提取，未提及则留空。"
                       "物料编码/单位按原文提取，无编码则填 null。"
                       "币种默认人民币；备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "采购合同",
        "description": "采购/销售合同，抽取合同编号、双方主体、金额、期限与付款方式",
        "priority": 22,
        "display_style": "card",
        "scenario": "采购履约 / 法务合规",
        "match_keywords": ["采购合同", "合同编号", "合同名称", "甲方", "乙方", "签订日期", "合同金额", "付款方式", "交付周期"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "contract_no", "label": "合同编号", "type": "string"},
            {"key": "contract_name", "label": "合同名称", "type": "string"},
            {"key": "subject_material", "label": "标的物料名称", "type": "string", "desc": "合同标的物料/产品名称，服务类留空"},
            {"key": "material_code", "label": "物料编码", "type": "string", "desc": "标的物料编码，无则留空"},
            {"key": "unit", "label": "单位", "type": "string", "desc": "标的计量单位，如 吨/件/个"},
            {"key": "qty", "label": "数量", "type": "number"},
            {"key": "party_a", "label": "甲方", "type": "string"},
            {"key": "party_b", "label": "乙方(供应商)", "type": "string"},
            {"key": "sign_date", "label": "签订日期", "type": "date"},
            {"key": "effective_date", "label": "生效日期", "type": "date"},
            {"key": "expiry_date", "label": "到期日期", "type": "date"},
            {"key": "amount", "label": "合同金额", "type": "number", "desc": "纯数字"},
            {"key": "payment_terms", "label": "付款方式", "type": "string", "desc": "如 月结30天/预付款30%"},
            {"key": "delivery_period", "label": "交付周期", "type": "string"},
            {"key": "penalty", "label": "违约责任", "type": "string"},
            {"key": "status", "label": "合同状态", "type": "string", "desc": "如 履行中/已完毕/已解除"},
            {"key": "currency", "label": "币种", "type": "string", "desc": "默认 人民币，跨境填 USD/EUR 等"},
            {"key": "tax_rate", "label": "税率", "type": "string", "desc": "如 13%，无则留空"},
            {"key": "sign_place", "label": "签约地点", "type": "string"},
            {"key": "remark", "label": "备注", "type": "string"},
        ],
        "prompt_extra": "合同金额输出纯数字；日期格式 YYYY-MM-DD；甲方乙方按文本明确主体填写。"
                       "标的物料编码/单位按原文提取，服务类合同留空。"
                       "币种默认人民币；税率/签约地点/备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "比价",
        "description": "采购比价/价格对比表，抽取标的、各供应商报价与推荐结论",
        "priority": 20,
        "display_style": "table",
        "scenario": "采购降本 / 供应商选择",
        "match_keywords": ["比价", "价格对比", "比价单", "最低价", "推荐供应商", "节约金额", "各供应商报价"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "compare_no", "label": "比价单号", "type": "string"},
            {"key": "compare_date", "label": "比价日期", "type": "date"},
            {"key": "item", "label": "比价标的", "type": "string", "desc": "物料或服务名称"},
            {"key": "material_code", "label": "物料编码", "type": "string", "desc": "标的物料编码，无则留空"},
            {"key": "spec", "label": "规格要求", "type": "string"},
            {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如 吨/件/个"},
            {"key": "quotes", "label": "各供应商报价", "type": "array", "desc": "数组，每项是一个供应商的报价，含 supplier/price", "items": [
                {"key": "supplier", "label": "供应商", "type": "string"},
                {"key": "price", "label": "报价", "type": "number"},
            ]},
            {"key": "lowest_supplier", "label": "最低价供应商", "type": "string"},
            {"key": "recommended", "label": "推荐供应商", "type": "string"},
            {"key": "saving", "label": "节约金额", "type": "number", "desc": "相对预算或最高价的节省，纯数字"},
            {"key": "conclusion", "label": "比价结论", "type": "string"},
            {"key": "currency", "label": "币种", "type": "string", "desc": "默认 人民币，跨境填 USD/EUR 等"},
            {"key": "qty", "label": "数量", "type": "number", "desc": "本次比价标的的数量，纯数字"},
            {"key": "remark", "label": "备注", "type": "string"},
        ],
        "prompt_extra": "报价输出纯数字；最低价供应商与推荐供应商按文本结论填写。"
                       "物料编码/单位按原文提取，无编码则填 null。"
                       "币种默认人民币；数量/备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "报价",
        "description": "供应商报价单，抽取报价单号、报价方、单价总价与有效期",
        "priority": 20,
        "display_style": "list",
        "scenario": "供应商报价评审",
        "match_keywords": ["报价单", "报价", "单价", "总价", "报价有效期", "报价方", "报价日期"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "quote_no", "label": "报价单号", "type": "string"},
            {"key": "quote_date", "label": "报价日期", "type": "date"},
            {"key": "supplier", "label": "报价方(供应商)", "type": "string"},
            {"key": "inquiry_no", "label": "关联询价单号", "type": "string"},
            {"key": "item", "label": "报价标的", "type": "string"},
            {"key": "material_code", "label": "物料编码", "type": "string", "desc": "标的物料编码，无则留空"},
            {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如 吨/件/个"},
            {"key": "unit_price", "label": "单价", "type": "number", "desc": "纯数字"},
            {"key": "total_price", "label": "总价", "type": "number", "desc": "纯数字"},
            {"key": "valid_until", "label": "报价有效期", "type": "date"},
            {"key": "delivery_date", "label": "交货期", "type": "date"},
            {"key": "payment_terms", "label": "付款条件", "type": "string"},
            {"key": "currency", "label": "币种", "type": "string", "desc": "默认 人民币，跨境填 USD/EUR 等"},
            {"key": "total_qty", "label": "合计数量", "type": "number", "desc": "纯数字"},
            {"key": "contact", "label": "联系人", "type": "string"},
            {"key": "remark", "label": "备注", "type": "string"},
            {
                "key": "items",
                "label": "报价明细",
                "type": "array",
                "desc": "多物料报价时数组，每行含 name/code/spec/unit/qty/unit_price/amount",
                "items": [
                    {"key": "name", "label": "物料名称", "type": "string"},
                    {"key": "code", "label": "物料编码", "type": "string", "desc": "物料/SKU 编码，无则留空"},
                    {"key": "spec", "label": "规格型号", "type": "string"},
                    {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如实提取：件/吨/个/箱/米等"},
                    {"key": "qty", "label": "数量", "type": "number"},
                    {"key": "unit_price", "label": "单价", "type": "number"},
                    {"key": "amount", "label": "金额", "type": "number"},
                ],
            },
        ],
        "prompt_extra": "单价与总价输出纯数字；报价有效期与交货期格式 YYYY-MM-DD。"
                       "单位与物料编码按原文提取，无编码则填 null。"
                       "币种默认人民币；合计数量、联系人、备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "询价",
        "description": "采购询价单，抽取询价单号、标的、规格数量与报价截止日",
        "priority": 19,
        "display_style": "list",
        "scenario": "采购询价 / 招标前期",
        "match_keywords": ["询价", "询价单", "询价日期", "报价截止", "询价方", "规格要求", "期望交货期"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "inquiry_no", "label": "询价单号", "type": "string"},
            {"key": "inquiry_date", "label": "询价日期", "type": "date"},
            {"key": "inquiry_party", "label": "询价方", "type": "string"},
            {"key": "item_desc", "label": "物料/服务描述", "type": "string"},
            {"key": "material_code", "label": "物料编码", "type": "string", "desc": "物料/SKU 编码，无则留空"},
            {"key": "spec", "label": "规格要求", "type": "string"},
            {"key": "unit", "label": "单位", "type": "string", "desc": "计量单位，如 吨/件/个"},
            {"key": "qty", "label": "数量", "type": "number"},
            {"key": "expected_delivery", "label": "期望交货期", "type": "date"},
            {"key": "deadline", "label": "报价截止日", "type": "date"},
            {"key": "suppliers", "label": "询价供应商", "type": "array", "desc": "被询价供应商名称数组"},
            {"key": "contact", "label": "联系人", "type": "string"},
            {"key": "remark", "label": "备注", "type": "string"},
        ],
        "prompt_extra": "日期格式 YYYY-MM-DD；数量输出纯数字；规格要求尽量保留原文关键参数。"
                       "物料编码/单位按原文提取，无编码则填 null。"
                       "联系人、备注按原文提取，无则填 null。",
        "is_fallback": False,
    },
    {
        "name": "供应商",
        "description": "供应商档案/准入信息，抽取供应商编号、联系方式、品类与评级",
        "priority": 17,
        "display_style": "card",
        "scenario": "供应商准入 / 档案维护",
        "match_keywords": ["供应商", "供应商编号", "供应商名称", "联系人", "主营品类", "合作状态", "评级", "准入日期"],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "supplier_no", "label": "供应商编号", "type": "string"},
            {"key": "name", "label": "供应商名称", "type": "string"},
            {"key": "contact", "label": "联系人", "type": "string"},
            {"key": "phone", "label": "联系方式", "type": "string"},
            {"key": "main_category", "label": "主营品类", "type": "string"},
            {"key": "cooperation_status", "label": "合作状态", "type": "string", "desc": "潜在/合格/冻结等"},
            {"key": "rating", "label": "评级", "type": "string", "desc": "如 A/B/C 或 优秀/合格"},
            {"key": "address", "label": "地址", "type": "string"},
            {"key": "bank_account", "label": "开户行及账号", "type": "string"},
            {"key": "admission_date", "label": "准入日期", "type": "date"},
        ],
        "prompt_extra": "供应商编号与名称严格按文本提取；合作状态与评级无明确表述则留空。",
        "is_fallback": False,
    },
    {
        "name": "通用文档",
        "description": "未命中专用模板时的兜底，抽取通用要素",
        "priority": 0,
        "display_style": "list",
        "scenario": "通用归档 / 智能识别",
        "match_keywords": [],
        "match_file_exts": [],
        "fields_schema": [
            {"key": "doc_type", "label": "文档类型", "type": "string", "desc": "你判断这是什么单据/文档"},
            {"key": "title", "label": "标题", "type": "string"},
            {"key": "doc_date", "label": "文档日期", "type": "date"},
            {"key": "org_names", "label": "涉及单位", "type": "array", "desc": "出现的公司/部门名称数组"},
            {"key": "amounts", "label": "涉及金额", "type": "array", "desc": "出现的金额数字数组"},
            {"key": "summary", "label": "内容摘要", "type": "string", "desc": "一句话概括"},
            {"key": "key_values", "label": "关键键值对", "type": "object", "desc": "识别到的其他字段名:值"},
        ],
        "prompt_extra": "这是兜底模板，尽量把文档里有价值的结构化信息都提取出来。",
        "is_fallback": True,
    },
]


def seed_templates(db: Session) -> int:
    """播种/同步默认模板（以本文件 DEFAULT_TEMPLATES 为唯一真源，幂等）：
    - 同名不存在 → 新增；
    - 同名已存在 → upsert：刷新 fields_schema / display_style / scenario / prompt_extra /
      match_keywords / match_file_exts / description / priority / is_fallback，
      但**保留**用户可能改过的 enabled、id、created_at。
    这样 templates.py 的字段结构（含数组明细 items、样式、场景）在重启后总是生效。
    """
    existing_rows = {
        row[0]: row[1]
        for row in db.execute(select(ExtractTemplate.name, ExtractTemplate.id)).all()
    }
    added = 0
    updated = 0
    for tpl in DEFAULT_TEMPLATES:
        name = tpl["name"]
        if name not in existing_rows:
            db.add(ExtractTemplate(**tpl))
            added += 1
            continue
        row = db.get(ExtractTemplate, existing_rows[name])
        if row is None:
            db.add(ExtractTemplate(**tpl))
            added += 1
            continue
        # upsert 默认模板定义（不覆盖 enabled）
        row.description = tpl.get("description", row.description)
        row.priority = tpl.get("priority", row.priority)
        row.match_keywords = tpl.get("match_keywords", row.match_keywords)
        row.match_file_exts = tpl.get("match_file_exts", row.match_file_exts)
        row.fields_schema = tpl.get("fields_schema", row.fields_schema)
        row.prompt_extra = tpl.get("prompt_extra", row.prompt_extra)
        row.display_style = tpl.get("display_style", row.display_style)
        row.scenario = tpl.get("scenario", row.scenario)
        row.is_fallback = tpl.get("is_fallback", row.is_fallback)
        updated += 1
    if added or updated:
        db.commit()
        logger.info("已播种 %d 个、同步 %d 个默认抽取模板", added, updated)
    return added


def match_template(
    db: Session, ocr_text: str, file_ext: str | None = None
) -> ExtractTemplate | None:
    """为一段 OCR 文本挑选最合适的模板"""
    templates = (
        db.execute(select(ExtractTemplate).where(ExtractTemplate.enabled.is_(True)))
        .scalars()
        .all()
    )
    if not templates:
        return None

    text = (ocr_text or "").lower()
    ext = (file_ext or "").lower()

    best: ExtractTemplate | None = None
    best_score = (0, -1)  # (命中关键词数, priority)
    fallback: ExtractTemplate | None = None

    for tpl in templates:
        if tpl.is_fallback and fallback is None:
            fallback = tpl

        exts = [e.lower() for e in (tpl.match_file_exts or [])]
        if exts and ext and ext not in exts:
            continue

        keywords = tpl.match_keywords or []
        if not keywords:
            continue

        hits = sum(1 for kw in keywords if kw and kw.lower() in text)
        if hits == 0:
            continue

        score = (hits, tpl.priority)
        if score > best_score:
            best_score = score
            best = tpl

    if best:
        logger.debug("模板匹配命中 %s（关键词命中 %d）", best.name, best_score[0])
        return best

    if fallback:
        logger.debug("未命中专用模板，使用兜底模板 %s", fallback.name)
    return fallback
