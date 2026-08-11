"""
app/config.py — 全局配置

设计要点：
1. 所有配置均可通过 .env 覆盖（pydantic-settings 自动读取），避免硬编码。
2. `COLLECTOR_MODE` 是本项目的核心开关：
     - mock    : 不依赖任何企业微信资源，用内置样例数据跑通全链路（开发/演示）
     - archive : 真实会话内容存档，需要 SDK 动态库 + corpid + secret + RSA 私钥
   两种模式共用后续的 OCR / 抽取 / 入库逻辑，切换零改动。
3. 数据库 URL 同时兼容 SQLite 与 PostgreSQL，切换只改连接串。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # 容忍 .env 里的无关变量，避免启动即崩
    )

    # ---------------- 应用 ----------------
    APP_NAME: str = "企业微信会话存档智能体"
    HOST: str = "0.0.0.0"
    # 8000=contract-ai-review 后端, 8001=invoice-ocr, 3000/3001 已占用，本项目用 8002
    PORT: int = 8002
    DEBUG: bool = True

    # ---------------- 数据库 ----------------
    # SQLite:     sqlite:///D:/Clow/projects/wecom-archive-agent/data/archive.db
    # PostgreSQL: postgresql+psycopg://user:pwd@host:5432/wecom_archive
    DATABASE_URL: str = f"sqlite:///{(BASE_DIR / 'data' / 'archive.db').as_posix()}"

    # ---------------- 采集模式 ----------------
    COLLECTOR_MODE: Literal["mock", "archive"] = "mock"

    # ---------------- 企业微信会话存档 ----------------
    WECOM_CORP_ID: str = ""
    # 管理后台 → 安全与管理 → 管理工具 → 会话内容存档 → Secret
    WECOM_ARCHIVE_SECRET: str = ""
    # SDK 动态库路径。Windows: WeWorkFinanceSdk.dll ；Linux: libWeWorkFinanceSdk_C.so
    WECOM_SDK_PATH: str = str(BASE_DIR / "data" / "sdk" / "WeWorkFinanceSdk.dll")
    # 企业自持 RSA 私钥（PKCS#1 / PKCS#8 PEM 文件），用于解密 encrypt_random_key
    WECOM_PRIVATE_KEY_PATH: str = str(BASE_DIR / "data" / "sdk" / "private_key.pem")
    # 多版本公钥场景：{"1": "/path/key_v1.pem", "2": "/path/key_v2.pem"}
    WECOM_PRIVATE_KEY_MAP: dict[str, str] = {}
    # 可选代理，如 socks5://10.0.0.1:8081
    WECOM_PROXY: str = ""
    WECOM_PROXY_PASSWD: str = ""
    WECOM_SDK_TIMEOUT: int = 30
    # 单次拉取条数，官方上限 1000
    WECOM_FETCH_LIMIT: int = 500
    # 企业微信 API 根地址（取 access_token / 群信息 / 成员列表等 HTTP 接口用）。
    # 默认官方地址；国密版/第三方服务商场景可改为对应域名。
    WECOM_API_BASE_URL: str = "https://qyapi.weixin.qq.com"

    # ---------------- 采集过滤 ----------------
    # 只采集指定群（roomid），留空=全部。逗号分隔
    FILTER_ROOM_IDS: str = ""
    # 是否跳过单聊（只要群聊）。本项目场景以群为主，默认 True
    ONLY_GROUP_CHAT: bool = True

    # ---------------- 媒体文件 ----------------
    MEDIA_ROOT: str = str(BASE_DIR / "data" / "media")
    # 单文件下载上限（MB），超过跳过，避免大视频撑爆磁盘
    MEDIA_MAX_SIZE_MB: int = 100
    # 需要下载并送 OCR 的媒体类型
    MEDIA_DOWNLOAD_TYPES: list[str] = ["image", "file", "emotion"]

    # ---------------- OCR ----------------
    OCR_ENABLED: bool = True
    # 送 OCR 的文件扩展名
    OCR_IMAGE_EXTS: list[str] = [".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".gif"]
    OCR_PDF_EXTS: list[str] = [".pdf"]
    # PDF 渲染 DPI（越高越准但越慢）
    OCR_PDF_DPI: int = 200
    # PDF 最多处理页数，防超大文件卡死
    OCR_PDF_MAX_PAGES: int = 20

    # ---------------- LLM 结构化抽取 ----------------
    EXTRACT_ENABLED: bool = True
    OLLAMA_BASE_URL: str = "http://172.17.6.18:11434"
    OLLAMA_MODEL: str = "qwen2.5:14b"
    OLLAMA_TIMEOUT: int = 180
    # LLM 输出温度，抽取任务要稳定，压到最低
    OLLAMA_TEMPERATURE: float = 0.1

    # ---------------- 调度 ----------------
    SCHEDULER_ENABLED: bool = True
    # 增量拉取间隔（秒）。会话存档非推送式，靠轮询 seq 实现"准实时"
    SYNC_INTERVAL_SECONDS: int = 60
    # 流水线（OCR+抽取）处理间隔（秒）
    PIPELINE_INTERVAL_SECONDS: int = 30
    # 单轮流水线最多处理多少个附件，防止一轮跑太久
    PIPELINE_BATCH_SIZE: int = 20

    # ---------------- 风险研判与分级预警 ----------------
    RISK_ENABLED: bool = True
    # 双引擎：关键词规则 + LLM 语义。关闭 LLM 仅用关键词（省 token）
    RISK_LLM_ENABLED: bool = True
    # 为省成本，仅当关键词未命中时才调 LLM（true=省，false=每条都跑 LLM）
    RISK_LLM_ONLY_WHEN_KEYWORD_MISS: bool = False
    # 风险扫描作业间隔（秒），独立于同步/流水线
    RISK_SCAN_INTERVAL_SECONDS: int = 45
    # 单轮风险扫描最多处理多少条未扫消息
    RISK_SCAN_BATCH: int = 50

    # 企微应用消息（精准推给具体人/部门用，与群机器人 Webhook 互补）
    WECOM_AGENT_ID: str = ""
    WECOM_AGENT_SECRET: str = ""

    # 邮件告警（SMTP 全局配置；具体收件人在「管理层投递目标」里按层配）
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_FROM: str = ""
    SMTP_TLS: bool = True

    @property
    def filter_room_id_set(self) -> set[str]:
        return {r.strip() for r in self.FILTER_ROOM_IDS.split(",") if r.strip()}

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


settings = Settings()
