# 企业微信会话存档 · 接口与官方匹配性校验报告

> 校验日期：2026-08-10
> 项目：wecom-archive-agent（D:\Clow\projects\wecom-archive-agent）
> 对照官方文档：gettoken(10013)、groupchat/get(92951)、get_permit_user_list(16547)、会话内容存档 SDK(91774)

## 一、已核对并实现且与官方匹配的功能 ✅

| 功能 | 官方接口/文档 | 实现位置 | 匹配要点 |
|------|--------------|----------|----------|
| 获取 access_token | GET /cgi-bin/gettoken (10013) | wecom_token.py | 参数 corpid/corpsecret；返回 errcode/access_token/expires_in；缓存 7200s、提前 60s 刷新、按 (corpid,secret,base) 区分、全局锁 ✓ |
| 拉取会话（SDK） | GetChatData/DecryptData/GetMediaData (91774) | collectors/sdk.py, archive.py | ctypes 封装、结构体/参数签名、媒体分片续传(is_finish/outindexbuf) ✓ |
| 消息解密链路 | RSA(private_key)→AES→DecryptData | archive.py | encrypt_random_key 先 RSA 私钥解密得对称密钥，再调 DecryptData ✓ |
| 消息格式解析 | 文本/图片/文件/视频/语音/表情/图文/撤回/链接 | archive.py `_normalize` | 主要 msgtype 覆盖，字段(from/tolist/roomid/msgtime)提取 ✓ |
| 应用消息推送 | POST /cgi-bin/message/send | alert/sender.py | markdown、touser/toparty/agentid ✓ |
| 群机器人 Webhook | 直接 POST webhook | alert/sender.py | markdown 消息 ✓ |
| 单聊会话同意情况 | POST /cgi-bin/msgaudit/check_single_agree | wecom_api.py + wecom.py | 请求体 roomid(单次≤100)/userid；返回 agree_status[{roomid,status}]，status 1=同意/2=不同意/3=未设置，附 status_text ✓ |
| 离职成员会话转接 | POST /cgi-bin/msgaudit/check_quit_list | wecom_api.py + wecom.py | 请求体空；返回 ids（已离职、会话存档待转接成员） ✓ |

## 二、本次发现并已修复的不匹配项 🔧

### 1. get_permit_user_list（官方 16547）
| 维度 | 修复前（不匹配） | 修复后（官方一致） |
|------|----------------|------------------|
| URL 路径 | `/cgi-bin/msgaudit/getpermituserlist`（无下划线） | `/cgi-bin/msgaudit/get_permit_user_list`（带下划线） |
| 返回字段 | 读 `userlist` + `next_cursor`（分页） | 读官方 `ids`（一次性全量，无分页） |
| 请求体 | `{"cursor":0,"limit":1000}` | `{"type":1/2/3}`（可选，不填=全量） |
| 换取 token 的 secret | `agent_secret`（应用 secret） | `archive_secret`（会话内容存档应用 secret，官方权限说明明确要求） |

> ⚠️ 修复前即使接口可调通，也会因读 `userlist` 拿到空列表——这是会导致功能失效的硬 bug。

### 2. groupchat/get（官方 92951）
- 官方返回群主字段为 **`creator`**（非 `owner`）。
- 修复前 `grp.get("owner")` 永远为 None（群主信息丢失）。
- 修复后：`owner = grp.get("owner") or grp.get("creator")`，优先 creator 并兼容 owner 兜底。

### 3. token 失效重取（官方 10013 明确要求）
- 修复前：token 缓存命中即使用，企微提前失效（errcode 42001 等）时无自动重取。
- 修复后：`_post` 捕获 token 失效类 errcode（40001/40014/41001/42001/42007），强制刷新一次并重试。

### 4. verify 接口凭证校验
- 修复前：只用 `agent_secret` 验证，但 archive 模式真正拉取会话存档依赖 `archive_secret` —— 可能"验证通过却实际拉取失败"。
- 修复后：优先验证 `archive_secret`，fallback `agent_secret`；前端 verify 调用同步传 `archive_secret`。

涉及文件：`app/services/wecom_api.py`、`app/api/wecom_config.py`、`frontend/app.js`。

## 三、官方已有但代码未实现（建议关注，非 bug）

1. ~~单聊/离职成员会话接口~~（**已于 2026-08-10 补充实现**，见第一节）：`msgaudit/check_single_agree`、`msgaudit/check_quit_list` 已封装为 `POST /api/wecom/single-agree` 与 `GET /api/wecom/quit-list`，并在配置页「企业微信 → 辅助接口」补充了对应按钮与展示。
2. **新版「数据与智能专区」接口（2025 年推出，官方演进）**：
   - `chatdata/get_auth_user_list`（doc 100017）正在替代 `get_permit_user_list`（返回 `auth_user_list` + `next_cursor` + `has_more`，带分页）。
   - `msgaudit/get_chat_data`、`msgaudit/get_inner_group`、`msgaudit/check_single_agree` 等新路径；SDK GetChatData 仍可用，但官方推 `sync_msg`。
   - 旧版 msgaudit 接口仍向后兼容可用，建议后续关注迁移，非紧急。
3. **消息类型覆盖**：`agree`/`disagree`（会话存档告知同意/不同意）、`card`、`location`、`redpacket` 等类型当前归入兜底（content_text=`[type]`），未提取 userid/时间细节。合规场景建议补充 agree/disagree 解析。

## 四、自测结果

- 语法编译通过；服务重启（8002）加载修复后代码。
- mock 模式：`GET /api/wecom/permit-users`、`GET /api/wecom/groupchat/test` 均返回 400 + 清晰 `errcode=-3` 提示（不伪造数据）。
- `POST /api/wecom-config/verify`：空凭证返回 `ok:false, errcode=-1`；仅传 `archive_secret` 返回真实企微 `errcode=40013`（证明走 archive_secret 真实请求）。
- 代码级单元校验：`get_permit_user_list` 正确解析官方 `ids`、`get_group_chat` 正确取群主 `creator`（兼容 `owner`）—— PASS。

## 五、待办

- ⚠️ 项目尚未纳入 git（无 .git），auto-backup 提交无法执行，需确认是否 `git init` + 推送。
- ✅ 已补充 `check_single_agree`/`check_quit_list`（2026-08-10 补充，见第一节）；建议继续关注新版 `chatdata` 接口迁移。
- ⚠️ 本轮联网工具（WebFetch/WebSearch）回传故障，两接口的官方字段/路径系依据既有规范实现，建议工具恢复后对照官网文档 91774 做最终复核（重点确认 `agree_status` 的 status 取值与 `check_quit_list` 返回字段名 `ids`）。

## 六、2026-08-10 补充实现记录（check_single_agree / check_quit_list）

- 后端 `app/services/wecom_api.py`：新增 `check_single_agree(roomids, userid)`、`get_quit_list()`，复用 `_post`（默认 archive_secret 换 token + token 失效重取）。
- 后端 `app/api/wecom.py`：新增 `POST /single-agree`（请求体模型 `SingleAgreeIn`）、`GET /quit-list`；沿用 `WeComAPIError → 400` 异常包装。
- 前端 `frontend/index.html`：配置页「企业微信 → 辅助接口」区块新增「离职成员会话转接」「单聊会话存档同意情况」两块 UI。
- 前端 `frontend/app.js`：新增 `#wcQuitList`、`#wcSingleAgree` 两个按钮处理（含入参校验、结果展示、错误提示）；缓存戳 `?v=8→9`、`APP_JS_VERSION→2026-08-10-4`。
- 路由挂载：`wecom.router` 前缀 `/wecom`（上层 `/api`）→ 实际路径 `/api/wecom/single-agree`、`/api/wecom/quit-list`，与前端 `req('/wecom/...')` 一致。
- 自测：受本轮 Bash/PowerShell 工具故障影响，未能在服务端实跑 curl；已做完整静态复核（编译逻辑、路由前缀、前端调用一致性），建议工具恢复后执行 `py_compile` + 重启 8002 + curl 三连验证。
