"""
app/collectors/sdk.py — 企业微信会话内容存档 C SDK 的 ctypes 封装

对应官方头文件 WeWorkFinanceSdk_C.h：

    typedef struct { char* buf; int len; } Slice_t;
    typedef struct {
        char* outindexbuf; int out_len;
        char* data;        int data_len;
        int is_finish;
    } MediaData_t;

    WeWorkFinanceSdk_t* NewSdk();
    int  Init(WeWorkFinanceSdk_t* sdk, const char* corpid, const char* secret);
    int  GetChatData(WeWorkFinanceSdk_t* sdk, unsigned long long seq, unsigned int limit,
                     const char* proxy, const char* passwd, int timeout, Slice_t* chatDatas);
    int  GetMediaData(WeWorkFinanceSdk_t* sdk, const char* indexbuf, const char* sdkFileid,
                      const char* proxy, const char* passwd, int timeout, MediaData_t* media);
    int  DecryptData(const char* encrypt_key, const char* encrypt_msg, Slice_t* msg);
    void DestroySdk(WeWorkFinanceSdk_t* sdk);
    ... 以及 Slice/MediaData 的构造与析构工具函数

⚠️ 内存管理是这层最容易出事的地方：
   NewSlice / NewMediaData 分配的内存必须显式 Free，否则长时间轮询会稳定泄漏。
   本模块用上下文管理器把 Free 绑定到 with 块，杜绝忘记释放。

SDK 下载（需与操作系统架构匹配）：
   Windows: https://wwcdn.weixin.qq.com/node/wework/images/sdk_win_v3.zip
   Linux  : https://wwcdn.weixin.qq.com/node/wwcomm/sdk_x86_v3_20250205.tgz
"""
from __future__ import annotations

import ctypes
import json
import logging
import platform
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


class WeWorkSdkError(RuntimeError):
    """SDK 调用失败。code 为企业微信返回的错误码"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- C 结构体
class Slice_t(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_char_p), ("len", ctypes.c_int)]


class MediaData_t(ctypes.Structure):
    _fields_ = [
        ("outindexbuf", ctypes.c_char_p),
        ("out_len", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_char)),
        ("data_len", ctypes.c_int),
        ("is_finish", ctypes.c_int),
    ]


def _default_lib_name() -> str:
    return "WeWorkFinanceSdk.dll" if platform.system() == "Windows" else "libWeWorkFinanceSdk_C.so"


class WeWorkFinanceSdk:
    """
    会话存档 SDK 句柄。线程安全（内部加锁）。

    典型用法：
        sdk = WeWorkFinanceSdk(lib_path, corp_id, secret)
        sdk.init()
        raw = sdk.get_chat_data(seq=0, limit=100)     # 密文
        plain = sdk.decrypt_data(aes_key, encrypt_msg) # 明文 dict
        blob = sdk.get_media_data(sdkfileid)           # bytes
        sdk.close()
    """

    def __init__(
        self,
        lib_path: str,
        corp_id: str,
        secret: str,
        proxy: str = "",
        proxy_passwd: str = "",
        timeout: int = 30,
    ):
        self.lib_path = lib_path
        self.corp_id = corp_id
        self.secret = secret
        self.proxy = proxy or ""
        self.proxy_passwd = proxy_passwd or ""
        self.timeout = timeout

        self._lib: ctypes.CDLL | None = None
        self._sdk = None
        self._lock = threading.RLock()
        self._initialized = False

    # ------------------------------------------------------------ 加载
    def _load_library(self) -> ctypes.CDLL:
        p = Path(self.lib_path)
        if not p.exists():
            raise WeWorkSdkError(
                f"未找到会话存档 SDK 动态库：{p}\n"
                f"请从企业微信官网下载 {_default_lib_name()} 并放到该路径。\n"
                f"Windows: https://wwcdn.weixin.qq.com/node/wework/images/sdk_win_v3.zip\n"
                f"Linux  : https://wwcdn.weixin.qq.com/node/wwcomm/sdk_x86_v3_20250205.tgz"
            )

        try:
            if platform.system() == "Windows":
                # dll 的依赖项（libcrypto/libssl）通常与主 dll 同目录，
                # 需把该目录加入搜索路径，否则报 [WinError 126] 找不到指定模块
                dll_dir = str(p.parent.resolve())
                if hasattr(ctypes, "windll"):
                    try:
                        ctypes.windll.kernel32.SetDllDirectoryW(dll_dir)
                    except Exception:  # noqa: BLE001
                        pass
                import os

                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(dll_dir)
                    except Exception:  # noqa: BLE001
                        pass

            lib = ctypes.CDLL(str(p.resolve()))
        except OSError as e:
            raise WeWorkSdkError(
                f"加载 SDK 动态库失败：{e}\n"
                f"常见原因：①32/64 位与 Python 解释器不匹配 ②缺少 OpenSSL 依赖库 "
                f"（v3.0 SDK 需 openssl 3.0，v2.0 需 openssl 1.1）"
            ) from e

        self._bind_signatures(lib)
        return lib

    @staticmethod
    def _bind_signatures(lib: ctypes.CDLL) -> None:
        """显式声明参数与返回类型。不声明的话 64 位指针会被截断成 int，导致段错误"""
        required = [
            "NewSdk", "Init", "GetChatData", "DecryptData", "GetMediaData", "DestroySdk",
            "NewSlice", "FreeSlice", "GetContentFromSlice", "GetSliceLen",
            "NewMediaData", "FreeMediaData", "GetOutIndexBuf", "GetData",
            "GetIndexLen", "GetDataLen", "IsMediaDataFinish",
        ]
        missing = [fn for fn in required if not hasattr(lib, fn)]
        if missing:
            raise WeWorkSdkError(f"SDK 动态库缺少必要导出函数：{missing}，请确认下载的是官方 C 版 SDK")

        lib.NewSdk.restype = ctypes.c_void_p
        lib.NewSdk.argtypes = []

        lib.Init.restype = ctypes.c_int
        lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]

        lib.GetChatData.restype = ctypes.c_int
        lib.GetChatData.argtypes = [
            ctypes.c_void_p,          # sdk
            ctypes.c_ulonglong,       # seq
            ctypes.c_uint,            # limit
            ctypes.c_char_p,          # proxy
            ctypes.c_char_p,          # passwd
            ctypes.c_int,             # timeout
            ctypes.POINTER(Slice_t),  # out
        ]

        lib.DecryptData.restype = ctypes.c_int
        lib.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Slice_t)]

        lib.GetMediaData.restype = ctypes.c_int
        lib.GetMediaData.argtypes = [
            ctypes.c_void_p,              # sdk
            ctypes.c_char_p,              # indexbuf
            ctypes.c_char_p,              # sdkfileid
            ctypes.c_char_p,              # proxy
            ctypes.c_char_p,              # passwd
            ctypes.c_int,                 # timeout
            ctypes.POINTER(MediaData_t),  # out
        ]

        lib.DestroySdk.restype = None
        lib.DestroySdk.argtypes = [ctypes.c_void_p]

        lib.NewSlice.restype = ctypes.POINTER(Slice_t)
        lib.NewSlice.argtypes = []
        lib.FreeSlice.restype = None
        lib.FreeSlice.argtypes = [ctypes.POINTER(Slice_t)]
        lib.GetContentFromSlice.restype = ctypes.POINTER(ctypes.c_char)
        lib.GetContentFromSlice.argtypes = [ctypes.POINTER(Slice_t)]
        lib.GetSliceLen.restype = ctypes.c_int
        lib.GetSliceLen.argtypes = [ctypes.POINTER(Slice_t)]

        lib.NewMediaData.restype = ctypes.POINTER(MediaData_t)
        lib.NewMediaData.argtypes = []
        lib.FreeMediaData.restype = None
        lib.FreeMediaData.argtypes = [ctypes.POINTER(MediaData_t)]
        lib.GetOutIndexBuf.restype = ctypes.POINTER(ctypes.c_char)
        lib.GetOutIndexBuf.argtypes = [ctypes.POINTER(MediaData_t)]
        lib.GetData.restype = ctypes.POINTER(ctypes.c_char)
        lib.GetData.argtypes = [ctypes.POINTER(MediaData_t)]
        lib.GetIndexLen.restype = ctypes.c_int
        lib.GetIndexLen.argtypes = [ctypes.POINTER(MediaData_t)]
        lib.GetDataLen.restype = ctypes.c_int
        lib.GetDataLen.argtypes = [ctypes.POINTER(MediaData_t)]
        lib.IsMediaDataFinish.restype = ctypes.c_int
        lib.IsMediaDataFinish.argtypes = [ctypes.POINTER(MediaData_t)]

    # ------------------------------------------------------------ 生命周期
    def init(self) -> None:
        with self._lock:
            if self._initialized:
                return
            if not self.corp_id or not self.secret:
                raise WeWorkSdkError("WECOM_CORP_ID / WECOM_ARCHIVE_SECRET 未配置")

            self._lib = self._load_library()
            self._sdk = self._lib.NewSdk()
            if not self._sdk:
                raise WeWorkSdkError("NewSdk() 返回空指针")

            ret = self._lib.Init(
                self._sdk, self.corp_id.encode("utf-8"), self.secret.encode("utf-8")
            )
            if ret != 0:
                self._lib.DestroySdk(self._sdk)
                self._sdk = None
                raise WeWorkSdkError(
                    f"SDK Init 失败，错误码={ret}。"
                    f"请核对 corpid 与「会话内容存档」的 Secret（注意不是应用 Secret）",
                    code=ret,
                )
            self._initialized = True
            logger.info("会话存档 SDK 初始化成功 corpid=%s", self.corp_id)

    def close(self) -> None:
        with self._lock:
            if self._sdk and self._lib:
                self._lib.DestroySdk(self._sdk)
                logger.info("会话存档 SDK 已释放")
            self._sdk = None
            self._initialized = False

    def __enter__(self):
        self.init()
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 内存守卫
    @contextmanager
    def _slice(self):
        sl = self._lib.NewSlice()
        try:
            yield sl
        finally:
            self._lib.FreeSlice(sl)

    @contextmanager
    def _media(self):
        md = self._lib.NewMediaData()
        try:
            yield md
        finally:
            self._lib.FreeMediaData(md)

    def _slice_bytes(self, sl) -> bytes:
        """按长度取内容。不能用 c_char_p 直接转——数据里可能含 \\0 会被提前截断"""
        length = self._lib.GetSliceLen(sl)
        if length <= 0:
            return b""
        ptr = self._lib.GetContentFromSlice(sl)
        return ctypes.string_at(ptr, length)

    def _ensure(self) -> None:
        if not self._initialized:
            self.init()

    # ------------------------------------------------------------ 拉取会话
    def get_chat_data(self, seq: int, limit: int = 500) -> list[dict]:
        """
        拉取密文会话记录。返回 chatdata 数组，每项含
        seq / msgid / publickey_ver / encrypt_random_key / encrypt_chat_msg

        注意：返回的消息从 seq+1 开始；limit 上限 1000。
        """
        self._ensure()
        limit = max(1, min(int(limit), 1000))

        with self._lock, self._slice() as sl:
            ret = self._lib.GetChatData(
                self._sdk,
                ctypes.c_ulonglong(seq),
                ctypes.c_uint(limit),
                self.proxy.encode("utf-8") if self.proxy else None,
                self.proxy_passwd.encode("utf-8") if self.proxy_passwd else None,
                ctypes.c_int(self.timeout),
                sl,
            )
            if ret != 0:
                raise WeWorkSdkError(f"GetChatData 失败，错误码={ret}（seq={seq}）", code=ret)
            payload = self._slice_bytes(sl)

        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise WeWorkSdkError(f"GetChatData 返回内容非法 JSON：{payload[:200]!r}") from e

        errcode = data.get("errcode", 0)
        if errcode != 0:
            raise WeWorkSdkError(
                f"GetChatData 业务错误 errcode={errcode} errmsg={data.get('errmsg')}", code=errcode
            )
        return data.get("chatdata") or []

    # ------------------------------------------------------------ 解密
    def decrypt_data(self, aes_key: str, encrypt_msg: str) -> dict:
        """
        用 RSA 解出的对称密钥解密消息体。
        aes_key = 企业私钥 RSA 解密 encrypt_random_key 后的明文字符串。
        """
        self._ensure()
        with self._lock, self._slice() as sl:
            ret = self._lib.DecryptData(
                aes_key.encode("utf-8"), encrypt_msg.encode("utf-8"), sl
            )
            if ret != 0:
                raise WeWorkSdkError(
                    f"DecryptData 失败，错误码={ret}。常见原因：私钥版本与 publickey_ver 不匹配",
                    code=ret,
                )
            payload = self._slice_bytes(sl)

        return json.loads(payload.decode("utf-8", errors="replace"))

    # ------------------------------------------------------------ 媒体下载
    def get_media_data(self, sdkfileid: str, max_bytes: int | None = None) -> bytes:
        """
        分片拉取媒体文件并拼接。

        SDK 每次最多返回 512KB，需用上一片返回的 outindexbuf 作为下一片的 indexbuf，
        直到 is_finish=1。首片 indexbuf 传空字符串。
        """
        self._ensure()
        chunks: list[bytes] = []
        index_buf = b""
        total = 0
        rounds = 0
        MAX_ROUNDS = 10000  # 512KB * 10000 ≈ 5GB，纯粹防御死循环

        while True:
            rounds += 1
            if rounds > MAX_ROUNDS:
                raise WeWorkSdkError(f"媒体分片轮次超限（sdkfileid={sdkfileid[:32]}...）")

            with self._lock, self._media() as md:
                ret = self._lib.GetMediaData(
                    self._sdk,
                    index_buf if index_buf else None,
                    sdkfileid.encode("utf-8"),
                    self.proxy.encode("utf-8") if self.proxy else None,
                    self.proxy_passwd.encode("utf-8") if self.proxy_passwd else None,
                    ctypes.c_int(self.timeout),
                    md,
                )
                if ret != 0:
                    raise WeWorkSdkError(
                        f"GetMediaData 失败，错误码={ret}（第 {rounds} 片）", code=ret
                    )

                data_len = self._lib.GetDataLen(md)
                if data_len > 0:
                    chunks.append(ctypes.string_at(self._lib.GetData(md), data_len))
                    total += data_len

                if max_bytes and total > max_bytes:
                    raise WeWorkSdkError(
                        f"媒体文件超过大小上限 {max_bytes} 字节，已中止下载"
                    )

                is_finish = self._lib.IsMediaDataFinish(md)
                if is_finish:
                    break

                idx_len = self._lib.GetIndexLen(md)
                index_buf = (
                    ctypes.string_at(self._lib.GetOutIndexBuf(md), idx_len) if idx_len > 0 else b""
                )
                if not index_buf:
                    # 未完成却拿不到续传游标，只能停，避免死循环
                    logger.warning("媒体分片未完成但 outindexbuf 为空，提前结束 total=%d", total)
                    break

        return b"".join(chunks)
