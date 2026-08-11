# 企业微信会话存档

把企业微信**群聊里的文件和图片**自动变成**可查询、可导出的业务数据库表**。

```
群聊消息/文件/图片  →  会话存档解密  →  OCR 文字识别  →  信息抽取  →  业务数据表
```

---

## 一、先说清楚一件事：群机器人做不到这个需求

调研结论（避免走弯路）：

| 方案 | 能否读群聊记录 | 能否拿群文件/图片 | 费用 |
|---|---|---|---|
| 群机器人（现名"消息推送"） | ❌ 只能发不能收 | ❌ | 免费 |
| 自建应用 + 回调 | ⚠️ 仅 @机器人 的那条消息 | ⚠️ 仅 @ 时携带的 | 免费 |
| **会话内容存档** | ✅ 全量静默采集 | ✅ 全量 | **付费** |

所以本项目走**会话内容存档**路线。开通条件：

1. 企业已完成**企业认证**
2. 在管理后台购买「会话内容存档」并**分配成员**（按人头/年计费）
3. 成员在手机端**点击同意**存档协议
4. 生成 RSA 密钥对，公钥上传后台，私钥自己保管
5. 下载官方 C SDK（`WeWorkFinanceSdk`），配置可信 IP

> 采购流程还没走完也不影响开发——项目内置 **mock 模式**，用自动生成的中文单据图片跑通完整链路，SDK 到位后改一个配置项即可切换。

---

## 二、快速开始

### 演示模式（零配置，立即可跑）

```bash
# Windows 双击
start.bat

# 或手动
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8002
```

打开 <http://127.0.0.1:8002> ，点右上角「立即同步」→「跑一轮流水线」，
几十秒后在「结构化数据」页就能看到从图片里抽出来的送货单、生产日报字段。

### 生产模式（接真实企业微信）

编辑 `.env`：

```ini
COLLECTOR_MODE=archive
WECOM_CORP_ID=ww1234567890abcdef
WECOM_ARCHIVE_SECRET=你的会话存档Secret
WECOM_SDK_PATH=D:/.../data/sdk/WeWorkFinanceSdk.dll
WECOM_PRIVATE_KEY_PATH=D:/.../data/sdk/private_key.pem
FILTER_ROOM_IDS=            # 留空=所有群；填 roomid 只采指定群
```

生成密钥对：

```bash
openssl genrsa -out private_key.pem 2048
openssl rsa -in private_key.pem -pubout -out public_key.pem   # 这个传到管理后台
```

改完在「系统」页点「重载采集器」，不用重启进程。

---

## 三、它是怎么工作的

### 三阶段流水线

```
阶段一 sync_messages()       每 60 秒    只做拉取+入库，几百毫秒
阶段二 process_attachments() 每 30 秒    下载→OCR→模型抽取，每个附件十几秒
阶段三 risk_scan()           每 45 秒    扫描未研判消息，命中规则→建事件→分级预警
```

**为什么要拆开**：会话存档**只保留最近 5 天**。如果把 OCR 和拉取写在一起，
一旦模型推理跑得慢，游标就推不动，超过 5 天的消息永久丢失。
拆开后哪怕 OCR 全挂了，原始消息也一条不少地躺在库里，随时能补跑。
风险扫描是**完全独立**的第三阶段——不依赖游标推进，历史消息回填重扫也不会扰动采集。

### 四层数据表

| 层 | 表 | 作用 |
|---|---|---|
| ① 原始层 | `chat_message` | 每条群消息，含 `raw_json` 保真兜底 |
| ② 附件层 | `attachment` | 媒体文件 + 三段独立状态机（下载/OCR/抽取） |
| ③ 识别层 | `ocr_result` | OCR 全文 + 带坐标的文字块 |
| ④ 业务层 | `extracted_record` | **最终产物**：按模板抽出的业务字段 JSON |
| 配置 | `extract_template` | 定义"什么文件抽哪些字段"，页面可改 |
| 运行时 | `sync_cursor` / `chat_room` | seq 断点续传 / 群档案 |

`extracted_record.fields_json` 可直接给 BI 展开成宽表：

```sql
-- SQLite
SELECT json_extract(fields_json,'$.delivery_no')  AS 单号,
       json_extract(fields_json,'$.total_amount') AS 金额
FROM extracted_record WHERE template_name='送货单';

-- PostgreSQL
SELECT fields_json->>'delivery_no', (fields_json->>'total_amount')::numeric
FROM extracted_record WHERE template_name='送货单';
```

### 加新业务不用改代码

在「抽取模板」页新建一个模板，填三样东西即可：

- **匹配关键词** — OCR 文本命中任一个就用这个模板（如 `质检报告,检验单,合格证`）
- **抽取字段** — key / 中文标签 / 类型（string、number、date、array、object）
- **补充规则** — 业务口径说明（可选）

建完可以直接在弹窗里粘一段文本点「试抽」，看效果不对就调关键词和字段说明，
调好了才让它上线跑真实数据。

---

## 四、风险研判与分级预警子系统（本项目的核心价值）

> 场景：系统主要用在**客户沟通群**和**采购沟通群**。任何消息触发风险，
> **不同群的风险自动预警给对应的不同管理层**——L1 业务主管、L2 部门总监、L3 总经理·合规。

### 双引擎检测（确定性 + 语义兜底）

每条消息/附件 OCR 文本，先过**关键词正则引擎**（秒级、零成本、确定性），
命不中再（或同时）过 **模型语义引擎**（复用本机 Ollama qwen2.5:14b，识别隐晦话术如"私下处理""账外"）。
两条引擎的结果按 `category` 归并去重，同一句话可能同时命中「私下交易」和「价格异常」两类。

### 分级路由：不同群 → 不同管理层

```
消息命中规则
  └─ 规则.alert_layers（显式指定层）  ← 优先级最高
       或 severity 兜底（low→L1, medium→L1+L2, high→L2+L3, critical→L1+L2+L3）
  └─ 逐层取其启用的投递目标（webhook / app / email / system）
  └─ 并行推送，每条落 AlertLog 回执，可失败重发
```

- **系统通知（system）永远必达**：风险页红点，不依赖任何外部配置。
- **按群隔离规则**：`RiskRule.scope_rooms` 填了 roomid 就只在该群生效，留空=全群。
  真实部署时在「风控配置」页按 roomid 隔离规则，即实现"客户群→L2、采购群→L3"的差异预警。

### 7 类风险（默认种子，开箱即检）

| 分类 | 默认严重度 | 路由层 | 典型话术 |
|---|---|---|---|
| 价格异常 | high | L2 | 不开票、账外处理 |
| 信息泄露 | medium | L2 | 把底价发给外面那家 |
| 合规风险 | high | L2 | 私下处理别走系统 |
| 回扣/利益输送 | critical | L3 | 给点回扣帮忙签了 |
| 客户投诉/舆情 | medium | L2 | 要打 12315 投诉 |
| 私下交易/飞单 | critical | L3 | 转你私账别走系统 |
| 竞品撬单 | medium | L2 | 看看竞品报价能不能更便宜 |

### 5 张表

| 表 | 作用 |
|---|---|
| `risk_rule` | 风险规则（分类/严重度/关键词/补充规则/scope_rooms/alert_layers） |
| `alert_layer` | 管理层（L1/L2/L3，可自定义） |
| `alert_target` | 每层的投递目标（webhook/app/email/system，可启用/禁用） |
| `risk_event` | 命中的风险事件（消息/分类/严重度/命中内容/处置状态/预警状态） |
| `alert_log` | 每次投递一条回执，便于审计与失败重发 |

### 页面

- **风险预警**：统计卡（按严重度/分类/群）+ 事件表（筛选/确认处置/回填重扫）+ 详情看投递回执
- **风控配置**：规则 CRUD（含按群隔离、严重度、关键词）、管理层与目标 CRUD（通道测试）

### 关键接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/risks/events` | 风险事件列表（分页/筛选） |
| GET | `/api/risks/stats` | 按严重度/分类/群聚合 |
| GET | `/api/risks/events/{id}` | 事件详情 |
| GET | `/api/risks/events/{id}/logs` | 投递回执（含路由到的层） |
| POST | `/api/risks/events/{id}/acknowledge` | 确认/处置 |
| POST | `/api/risks/events/{id}/resend` | 重新发送预警 |
| POST | `/api/risks/rescan` | 回填重扫（指定群或全部） |
| GET/POST | `/api/risks/rules` | 规则列表 / 新建 |
| PATCH/DELETE | `/api/risks/rules/{id}` | 改 / 删规则 |
| GET/POST | `/api/risks/layers`、`/targets` | 管理层与目标 CRUD |
| POST | `/api/risks/layers/{id}/test` | 测试该层投递通道 |

---

## 五、模型配置（通用化，界面可配）

模型连接不再写死在 `.env` 里。连接信息下沉到数据库，在「模型配置」页面就能增删改、测试、切换，
支持**本地模型**和**外部模型**两类：

- **本地 Ollama**：provider 选 `ollama`，Base URL 填 `http://host:11434`，走原生 `/api/chat`（带 `format=json` 强约束）。
- **外部 OpenAI 兼容**：provider 选 `openai`，Base URL 填任意 OpenAI 兼容端点
  （OpenAI / DeepSeek / 通义 / vLLM / Azure OpenAI 等，如 `https://api.deepseek.com/v1`），
  走 `/v1/chat/completions` + `Authorization: Bearer`，自动带 `response_format=json_object`（端点不支持时自动退避）。

**按用途（role）路由**：每个连接可勾选服务于哪些用途——

- `extract` 结构化抽取（附件 OCR 文本 → 业务字段）
- `risk` 风险研判（模型语义兜底）

运行时按 role 解析"启用且勾选了该用途"的连接；同一用途有多个连接时取最早创建的，
都没有则回退到「默认连接」。改完即时生效，路由有 30s 短缓存。

**安全**：API Key 入库存储，但所有列表/详情接口一律**脱敏不回传明文**；编辑时 API Key 留空表示「不修改」。

首次启动会自动播种一条「本地 Ollama」默认连接（取值来自 `.env` 的 `OLLAMA_BASE_URL` / `OLLAMA_MODEL`），
同时服务于 `extract` 与 `risk`，保证开箱即用、行为与改造前一致。

---

## 六、技术选型说明

| 组件 | 选择 | 原因 |
|---|---|---|
| OCR | RapidOCR（PP-OCRv4 + ONNXRuntime） | 纯 CPU 可跑，中文准确率高，不用装 PaddlePaddle |
| PDF | PyMuPDF | 优先抽文本层，是扫描件才渲染图片走 OCR，省时间 |
| 模型 | Ollama + qwen2.5:14b（本机 172.17.6.18） | 数据不出内网；`format=json` 强约束输出 |
| 数据库 | SQLAlchemy 2.0 | SQLite 起步，生产切 PostgreSQL 只改连接串 |
| 调度 | APScheduler | `max_instances=1` 防止两轮同时推游标 |
| 前端 | 原生 JS 单页 | 后端直接托管，不占额外端口，不用 npm |

**几个稳定性上的处理**：

- ctypes 调 C SDK 时显式声明所有 `argtypes/restype`，防 64 位指针被截断成 32 位
- SDK 返回的 `Slice_t`/`MediaData_t` 用上下文管理器包住，异常路径也保证 Free
- 本地 14B 模型 JSON 偶尔会漏括号 → 栈式括号修复 + 多策略解析候选
- 媒体分片下载用 `outindexbuf` 续传，带 MAX_ROUNDS 防死循环
- `msgid` 唯一约束 + `IntegrityError` 捕获，并发拉取不会写重

---

## 七、接口清单（28 + 风险 19 + 模型 7 个）

完整文档 <http://127.0.0.1:8002/docs>

**结构化数据（核心）**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/records` | 列表，支持模板/状态/时间/全文过滤 |
| GET | `/api/records/flatten` | **按模板展开成宽表**，前端表格直接渲染 |
| GET | `/api/records/export` | **导出 Excel** |
| PATCH | `/api/records/{id}` | 人工复核：改字段即标记已复核 |

**模板**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/templates` | 列表 / 新建 |
| PATCH/DELETE | `/api/templates/{id}` | 改 / 删（已产数据的自动转停用） |
| POST | `/api/templates/try-run` | **在线试抽**，贴文本立即看结果 |

**消息与附件**

`/api/messages`、`/api/messages/{id}`、`/api/rooms`、`/api/attachments`、
`/api/attachments/{id}/ocr`、`/api/attachments/{id}/file`、`/api/attachments/{id}/retry`、`/api/ocr-results`

**系统**

`/api/system/health`、`/stats`、`/sync`、`/pipeline/run`、`/pipeline/reset-failed`、
`/cursor`、`/scheduler/{pause|resume}`、`/collector/reload`、`/config`

**模型配置（通用化，本地/外部）**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/models` | 连接列表（api_key 脱敏） |
| POST | `/api/models` | 新建连接（slug 自生成） |
| GET | `/api/models/{id}` | 详情 |
| PATCH | `/api/models/{id}` | 更新（api_key 留空=不修改） |
| DELETE | `/api/models/{id}` | 删除 |
| POST | `/api/models/{id}/test` | 连通性 + 样例 JSON 自检 |
| POST | `/api/models/probe` | 对未保存配置做自检（不落库） |
| POST | `/api/models/fetch-models` | 按 provider/base_url/api_key 拉取远端模型清单 |
| GET | `/api/models/roles` | 角色（extract/risk）→ 当前生效连接 绑定关系 |

---

## 八、目录结构

```
wecom-archive-agent/
├── app/
│   ├── main.py              入口：建表、播种模板/风控默认值、起调度、挂前端
│   ├── config.py            配置（.env 覆盖，含 RISK_*/WECOM_AGENT_*/SMTP_*）
│   ├── scheduler.py         三个定时作业（sync / pipeline / risk_scan）
│   ├── collectors/          采集层（可插拔）
│   │   ├── base.py          抽象接口 + 统一消息模型
│   │   ├── archive.py       真实会话存档
│   │   ├── mock.py          演示数据（PIL 画中文单据 + 风险话术样本）
│   │   ├── sdk.py           C SDK 的 ctypes 封装
│   │   └── crypto.py        RSA 解密 random_key
│   ├── services/
│   │   ├── ocr/engine.py    RapidOCR + PDF
│   │   ├── extract/         llm.py(兼容层，按 role 委托) / 模板 / 抽取器
│   │   ├── llm/             client.py(通用客户端：ollama+openai 双协议/路由/自检) + seed.py
│   │   ├── risk/            categories.py(分类/关键词库/默认路由) + detector.py(双引擎) + seed.py
│   │   ├── alert/sender.py  多通道投递（webhook/app/email/system）+ 回执
│   │   └── pipeline.py      端到端编排（含 detect_and_store / risk_scan / risk_rescan）
│   ├── models/
│   │   ├── entities.py      四层数据表 + chat_message.risk_scanned
│   │   ├── risk.py          5 张风险表（rule/layer/target/event/log）
│   │   └── model_config.py  模型连接配置表（provider/base_url/api_key/roles…）
│   └── api/
│       ├── models.py        模型配置 9 个接口
│       ├── risks.py         风险预警 16 个接口
│       └── ...              其余结构化数据接口
├── frontend/                管理页（风险预警 + 风控配置 两个 tab）
├── data/
│   ├── archive.db           SQLite
│   ├── media/               下载的群文件（按 群/日期 分目录）
│   ├── fixtures/            mock 演示图片
│   └── sdk/                 放 SDK 动态库和私钥
├── .env.example             配置样例（含风险预警子系统全部配置项）
└── start.bat
```

---

## 九、常见问题

**Q：日志刷 `maximum number of running instances reached`？**
正常保护。说明上一轮 OCR+模型推理还没跑完，新一轮被跳过了，不会丢任务。
持续出现说明处理能力不够，可调大 `PIPELINE_INTERVAL_SECONDS` 或换更小的模型。

**Q：会话存档只有 5 天，历史数据怎么办？**
没办法补，存档服务本身就只留 5 天。所以要**尽早部署**，游标 `sync_cursor.seq`
一定要跟着数据库一起备份——游标丢了就等于丢数据。

**Q：能不能只采某几个群？**
`.env` 里 `FILTER_ROOM_IDS=roomid1,roomid2`。先跑一次全量，
在「概览」页的群列表里抄 roomid，再填回去。

**Q：抽取不准怎么办？**
先在「抽取模板」页点「试抽」定位是 OCR 没识别出来，还是补充规则没写清楚。
前者调 `OCR_PDF_DPI`，后者在字段的 desc 和「补充规则」里把业务口径写明白。

**Q：切 PostgreSQL 要改什么？**
```bash
pip install "psycopg[binary]"
# .env
DATABASE_URL=postgresql+psycopg://user:pwd@host:5432/wecom_archive
```
代码零改动，表会自动建。

**Q：不同群想预警给不同的管理层，怎么配？**
1. 「风控配置 → 规则」里，把规则按 `scope_rooms` 隔离：
   采购群规则填采购群 roomid + `alert_layers=["L3"]`，客户群规则填客户群 roomid + `alert_layers=["L2"]`。
2. 「风控配置 → 管理层」里给 L2/L3 配置真实通道（企微群机器人 Webhook / 应用消息 / 邮件）。
3. 不填 `scope_rooms` 的规则全群生效；`alert_layers` 留空则按严重度自动兜底路由。

**Q：风险扫描会不会拖慢采集/游标？**
不会。风险扫描是独立的第三阶段（45 秒一次），只读 `risk_scanned=False` 的消息，
扫完打标，绝不碰 `sync_cursor`。历史消息回填重扫（`POST /api/risks/rescan`）也不扰动采集。

---

## 十、端口约定

| 端口 | 项目 |
|---|---|
| 8000 | contract-ai-review 后端 |
| 8001 | invoice-ocr |
| **8002** | **本项目** |
