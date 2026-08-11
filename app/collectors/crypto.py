"""
app/collectors/crypto.py — encrypt_random_key 的 RSA 解密

官方流程（文档明确规定）：
  a) encrypt_random_key 先做 base64 decode  → str1
  b) 用 publickey_ver 对应版本的企业私钥，RSA **PKCS#1 v1.5** 解密 str1 → str2
  c) str2 与 encrypt_chat_msg 一起传给 SDK 的 DecryptData，得到明文

坑点：
  ① 必须是 PKCS#1 v1.5，不是 OAEP。用 OAEP 会解出乱码或直接抛错。
  ② 密钥模长 2048 bit。
  ③ 企业可能轮换公钥，消息里的 publickey_ver 指明该用哪个版本的私钥，
     所以支持"版本 → 私钥文件"的映射；只配单把私钥时对所有版本生效。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

logger = logging.getLogger(__name__)


class PrivateKeyError(RuntimeError):
    pass


class RandomKeyDecryptor:
    """
    管理多版本私钥并解密 encrypt_random_key。

    :param default_key_path: 默认私钥（未匹配到版本时使用）
    :param key_map: {"1": "/path/v1.pem", "2": "/path/v2.pem"}
    """

    def __init__(self, default_key_path: str = "", key_map: dict[str, str] | None = None):
        self.default_key_path = default_key_path
        self.key_map = key_map or {}
        self._cache: dict[str, RSA.RsaKey] = {}

    # ------------------------------------------------------------------
    def _load_key(self, path: str) -> RSA.RsaKey:
        if path in self._cache:
            return self._cache[path]

        p = Path(path)
        if not p.exists():
            raise PrivateKeyError(
                f"RSA 私钥文件不存在：{p}\n"
                f"该私钥需与企业微信管理后台「会话内容存档」里填写的公钥配对。\n"
                f"生成命令：\n"
                f"  openssl genrsa -out private_key.pem 2048\n"
                f"  openssl rsa -in private_key.pem -pubout -out public_key.pem\n"
                f"然后把 public_key.pem 内容粘贴到管理后台。"
            )

        try:
            key = RSA.import_key(p.read_bytes())
        except (ValueError, IndexError, TypeError) as e:
            raise PrivateKeyError(f"私钥解析失败（{p}）：{e}，请确认是 PEM 格式的 RSA 私钥") from e

        if not key.has_private():
            raise PrivateKeyError(f"{p} 是公钥而非私钥，解密需要私钥")
        if key.size_in_bits() != 2048:
            logger.warning("私钥模长 %d bit，企业微信要求 2048 bit", key.size_in_bits())

        self._cache[path] = key
        return key

    def _resolve_path(self, publickey_ver: int | str | None) -> str:
        ver = str(publickey_ver) if publickey_ver is not None else ""
        if ver and ver in self.key_map:
            return self.key_map[ver]
        if self.default_key_path:
            return self.default_key_path
        if self.key_map:
            # 没配默认值时，取版本号最大的那把
            latest = max(self.key_map.keys(), key=lambda k: (len(k), k))
            return self.key_map[latest]
        raise PrivateKeyError("未配置任何 RSA 私钥（WECOM_PRIVATE_KEY_PATH / WECOM_PRIVATE_KEY_MAP）")

    # ------------------------------------------------------------------
    def decrypt(self, encrypt_random_key: str, publickey_ver: int | str | None = None) -> str:
        """返回 SDK DecryptData 所需的对称密钥字符串"""
        key = self._load_key(self._resolve_path(publickey_ver))

        try:
            ciphertext = base64.b64decode(encrypt_random_key)
        except Exception as e:  # noqa: BLE001
            raise PrivateKeyError(f"encrypt_random_key 不是合法 base64：{e}") from e

        # sentinel 用于 PKCS1_v1_5 解密失败时的返回值判定
        sentinel = object()
        plain = PKCS1_v1_5.new(key).decrypt(ciphertext, sentinel)

        if plain is sentinel or plain is None:
            raise PrivateKeyError(
                f"RSA 解密失败（publickey_ver={publickey_ver}）。"
                f"通常是私钥与后台公钥不配对，或该消息用的是其他版本公钥加密"
            )

        return plain.decode("utf-8", errors="replace")
