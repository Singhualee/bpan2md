"""带退避重试的 HTTP 辅助。

批量跑几小时的时候，一次瞬时的 5xx 或连接重置不应该让整个任务失败——
但也不能无脑重试所有请求：**提交转写任务是会花钱的**，一次连接错误
可能只是响应丢了（任务其实已经创建），这时重试会产生重复计费。
所以这里区分两类：

- 幂等请求（查询状态、下载结果、上传对象）：连接错误和 5xx 都重试；
- 有副作用的请求（提交任务）：只重试明确的 429/5xx，连接错误直接抛出，
  由上层决定怎么处理。
"""

from __future__ import annotations

import random
import time

import requests

# 这些状态码代表"服务端暂时不行"，重试有意义
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class RetryableStatus(RuntimeError):
    def __init__(self, response: requests.Response):
        super().__init__(f"HTTP {response.status_code}: {response.text[:200]}")
        self.response = response
        self.status = response.status_code


def is_retryable(exc: BaseException) -> bool:
    """只有「服务端暂时不行」才算可重试。

    RetryableStatus 也校验一次状态码：调用方可能自己构造它，
    不能因为类型对了就当成可重试（403/404 重试多少次都是白费）。
    """
    if isinstance(exc, RetryableStatus):
        return exc.status in RETRY_STATUS
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def with_retry(fn, *, attempts: int = 4, base_delay: float = 0.8,
               max_delay: float = 20.0, retry_connection: bool = True,
               log=None, what: str = "请求"):
    """执行 fn()，遇到可重试的错误就退避重试。

    fn 里抛 RetryableStatus / ConnectionError / Timeout 会被重试；
    retry_connection=False 时连接类错误不重试（用于有副作用的 POST）。
    """
    delay = base_delay
    for i in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - 需要按类型判断后重新抛出
            if isinstance(exc, KeyboardInterrupt):
                raise
            retryable = is_retryable(exc)
            if isinstance(exc, (requests.ConnectionError, requests.Timeout)) and not retry_connection:
                retryable = False
            if not retryable or i == attempts:
                raise
            wait = min(delay, max_delay) * (1 + random.random() * 0.3)
            if log:
                log(f"[retry] {what} 第 {i} 次失败（{type(exc).__name__}: "
                    f"{str(exc)[:80]}），{wait:.1f}s 后重试")
            time.sleep(wait)
            delay *= 2
    raise AssertionError("unreachable")


def _check(resp: requests.Response) -> requests.Response:
    if resp.status_code in RETRY_STATUS:
        raise RetryableStatus(resp)
    return resp


def get(url: str, *, log=None, what: str = "GET", **kw) -> requests.Response:
    kw.setdefault("timeout", 60)
    return with_retry(lambda: _check(requests.get(url, **kw)), log=log, what=what)


def post(url: str, *, log=None, what: str = "POST", retry_connection: bool = False, **kw):
    kw.setdefault("timeout", 120)
    return with_retry(
        lambda: _check(requests.post(url, **kw)),
        log=log, what=what, retry_connection=retry_connection,
    )


def put(url: str, *, log=None, what: str = "PUT", **kw) -> requests.Response:
    kw.setdefault("timeout", 600)
    return with_retry(lambda: _check(requests.put(url, **kw)), log=log, what=what)


def delete(url: str, *, log=None, what: str = "DELETE", **kw) -> requests.Response:
    kw.setdefault("timeout", 60)
    return with_retry(lambda: _check(requests.delete(url, **kw)), log=log, what=what)
