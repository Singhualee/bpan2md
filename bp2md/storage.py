"""把抽取出来的音频放到一个「公网可访问的 URL」上。

为什么需要这一步：百炼/听悟的录音文件识别都要求给一个公网 http(s) URL，
它们不支持直接上传本地文件（唯一支持直传的 qwen3-asr-flash 只有
10MB / 5 分钟，长音频用不了）。

为什么不用官方 oss2 SDK：它依赖 crcmod，而 crcmod 在 Windows 上没有
预编译 wheel，pip 会尝试源码编译并失败。OSS 的签名算法很简单，这里直接
用标准库实现，反而更省事、更可控。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

from . import http
from .config import Config


class StorageError(RuntimeError):
    pass


# 有些 Bucket 开了「阻止公共访问」，不允许把 object 设成 public-read。
# 记下这些桶，同一轮里后续文件直接走「私有 + 签名链接」，不再白撞一次、
# 也不再对每个文件重复刷一遍提示。
_BLOCKED_PUBLIC_ACL: set[str] = set()

# 阿里云 OSS 的对应错误：403 + Put public object acl is not allowed（EC 0016-00000901）
_PUBLIC_ACL_DENIED = ("Put public object acl is not allowed", "0016-00000901")


def _public_acl_blocked(resp) -> bool:
    if getattr(resp, "status_code", None) != 403:
        return False
    body = getattr(resp, "text", "") or ""
    return any(mark in body for mark in _PUBLIC_ACL_DENIED)


def _endpoint_host(endpoint: str) -> str:
    host = endpoint.strip()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    known = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".amr": "audio/amr",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
    }
    return known.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


class OssStorage:
    """阿里云 OSS 上传（签名 V1）。也可以用任何 S3 兼容服务，只要改 endpoint。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.host = _endpoint_host(cfg.oss_endpoint)
        # 这个桶是否拒绝把对象设成公共读（决定返回的是公开 URL 还是签名 URL）。
        # 调用方（比如 doctor，它把上传日志静音了）可以据此自己提示用户。
        self.public_acl_blocked = False
        if not self.host:
            raise StorageError("OSS_ENDPOINT 没填（例如 oss-cn-beijing.aliyuncs.com）")
        if not cfg.oss_bucket:
            raise StorageError("OSS_BUCKET 没填")
        if not (cfg.oss_ak_id and cfg.oss_ak_secret):
            raise StorageError("OSS_AK_ID / OSS_AK_SECRET 没填")

    # ---------- 签名 ----------
    def _sign(self, string_to_sign: str) -> str:
        digest = hmac.new(
            self.cfg.oss_ak_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _object_url(self, key: str) -> str:
        return f"https://{self.cfg.oss_bucket}.{self.host}/{quote(key)}"

    def signed_url(self, key: str, expires: int | None = None) -> str:
        """生成一个带签名的临时 GET URL（桶是私有时用）。"""
        ttl = expires or self.cfg.oss_url_expires
        when = int(time.time()) + ttl
        string_to_sign = (
            f"GET\n\n\n{when}\n"
            f"/{self.cfg.oss_bucket}/{key}"
        )
        sig = quote(self._sign(string_to_sign), safe="")
        return (f"{self._object_url(key)}?OSSAccessKeyId={self.cfg.oss_ak_id}"
                f"&Expires={when}&Signature={sig}")

    # ---------- 上传 ----------
    def _put(self, key: str, data: bytes, content_type: str, *,
             acl: bool, log=None):
        oss_headers = {"x-oss-object-acl": "public-read"} if acl else {}
        # Date 参与签名，重试时必须重新生成，否则会因为时间偏差被拒
        date = formatdate(usegmt=True)
        canonical_oss = "".join(
            f"{k.lower()}:{v}\n" for k, v in sorted(oss_headers.items())
        )
        string_to_sign = (
            f"PUT\n\n{content_type}\n{date}\n"
            f"{canonical_oss}"
            f"/{self.cfg.oss_bucket}/{key}"
        )
        headers = {
            "Date": date,
            "Content-Type": content_type,
            "Authorization": f"OSS {self.cfg.oss_ak_id}:{self._sign(string_to_sign)}",
            **oss_headers,
        }
        return http.put(self._object_url(key), data=data, headers=headers,
                        log=log, what="上传 OSS")

    def upload(self, local: Path, key: str | None = None, log=None) -> str:
        key = key or f"{self.cfg.oss_prefix.strip('/')}/{local.name}"
        data = local.read_bytes()
        content_type = _content_type(local)

        want_public = (self.cfg.oss_public_read
                       and self.cfg.oss_bucket not in _BLOCKED_PUBLIC_ACL)
        resp = self._put(key, data, content_type, acl=want_public, log=log)

        if want_public and _public_acl_blocked(resp):
            # 桶开了「阻止公共访问」。这不是用户配错了，不该让他去改桶设置——
            # 自动降级成"私有上传 + 临时签名链接"，对转写服务来说效果完全一样。
            _BLOCKED_PUBLIC_ACL.add(self.cfg.oss_bucket)
            self.public_acl_blocked = True
            if log:
                log("")
                log("! 这个 Bucket 开启了「阻止公共访问」，不允许把文件设成公共读。")
                log("  （这不是你配错了。阿里云现在默认就会打开这个开关。）")
                log("  已自动改用「私有上传 + 临时签名链接」，效果一样，无需任何改动。")
                log("  想以后不再看到这条提示：设置里把「音频上传后是否公开可读」改成「私有」。")
            want_public = False
            resp = self._put(key, data, content_type, acl=False, log=log)

        if resp.status_code not in (200, 203):
            raise StorageError(
                f"OSS 上传失败 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return self._object_url(key) if want_public else self.signed_url(key)

    def delete(self, key: str) -> None:
        """清理临时文件（失败不影响主流程）。"""
        date = formatdate(usegmt=True)
        string_to_sign = f"DELETE\n\n\n{date}\n/{self.cfg.oss_bucket}/{key}"
        headers = {
            "Date": date,
            "Authorization": f"OSS {self.cfg.oss_ak_id}:{self._sign(string_to_sign)}",
        }
        try:
            http.delete(self._object_url(key), headers=headers, timeout=30)
        except Exception:
            pass


class ManualStorage:
    """你自己已经有公网托管时用：只拼 URL，不上传。

    把音频拷贝到 PUBLIC_LOCAL_DIR（如果配了），返回 PUBLIC_BASE_URL/文件名。
    适合「已经有 nginx / 对象存储对外开放目录」的情况。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.public_base_url:
            raise StorageError("PUBLIC_BASE_URL 没填")
        self.local_dir = cfg.public_local_dir or None

    def upload(self, local: Path, key: str | None = None) -> str:
        name = local.name
        if self.local_dir:
            dst = Path(self.local_dir) / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(local.read_bytes())
        return f"{self.cfg.public_base_url.rstrip('/')}/{quote(name)}"

    def delete(self, key: str) -> None:
        return None


def make_storage(cfg: Config):
    if cfg.public_base_url:
        return ManualStorage(cfg)
    return OssStorage(cfg)
