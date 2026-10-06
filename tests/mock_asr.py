"""本地 mock 服务：用来在没有任何密钥的情况下，真实地驱动 ASR 客户端代码。

为什么需要它：`bp2md/asr.py` 里的 `BailianAsr` / `SiliconFlowAsr` 以前**从没被
执行过**——所有流水线测试都在 `make_asr` 那一层就打桩了。于是轮询循环、状态
判断、`transcription_url` 下载、结果解析（也就是最初那份方案里错得最离谱的
地方）全是未测代码。

这里起一个真正的 HTTP 服务，让客户端走完整的网络请求。返回的报文结构
逐字段照抄阿里云官方文档，不是我自己编的形状。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 官方文档给出的 transcriptionPath 解析协议原文结构
OFFICIAL_RESULT = {
    "file_url": "https://example.com/a.mp3",
    "properties": {
        "audio_format": "mp3",
        "channels": [0],
        "original_sampling_rate": 16000,
        "original_duration_in_milliseconds": 3834,
    },
    "transcripts": [
        {
            "channel_id": 0,
            "content_duration_in_milliseconds": 2480,
            "text": "Hello World，这里是阿里巴巴语音实验室。",
            "sentences": [
                {"begin_time": 760, "end_time": 3240,
                 "text": "Hello World，这里是阿里巴巴语音实验室。",
                 "sentence_id": 1, "speaker_id": 0},
                {"begin_time": 3240, "end_time": 3834,
                 "text": "第二句话。", "sentence_id": 2, "speaker_id": 1},
            ],
        }
    ],
}


class _State:
    def __init__(self, **kw):
        self.polls_before_ready = kw.get("polls_before_ready", 1)
        self.task_fail = kw.get("task_fail", False)
        self.subtask_fail = kw.get("subtask_fail", False)
        self.missing_url = kw.get("missing_url", False)
        self.flaky_submit = kw.get("flaky_submit", 0)      # 前 N 次提交返回 500
        self.flaky_poll = kw.get("flaky_poll", 0)          # 前 N 次轮询返回 500
        self.no_task_id = kw.get("no_task_id", False)
        self.result = kw.get("result", OFFICIAL_RESULT)
        self.sf_segments = kw.get("sf_segments", None)      # siliconflow verbose_json
        self.sf_text = kw.get("sf_text", None)
        self.counts = {"submit": 0, "poll": 0, "result": 0, "sf": 0}
        self.last_headers: dict = {}          # 最近一次提交的请求头
        self.last_get_headers: dict = {}      # 最近一次查询的请求头
        self.last_body: dict = {}
        self.last_multipart = b""


class MockAsrServer:
    """用法：

        srv = MockAsrServer(polls_before_ready=2)
        base = srv.start()          # 形如 http://127.0.0.1:54321/api/v1
        ...                         # 把 cfg.dashscope_base 指过去
        srv.stop()
    """

    def __init__(self, **kw):
        self.state = _State(**kw)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------
    def start(self) -> str:
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):   # 别把访问日志打到测试输出里
                pass

            def _send(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _record(self, body: bytes = b"", kind: str = "submit"):
                target = state.last_headers if kind == "submit" else state.last_get_headers
                target.clear()
                target.update({k: v for k, v in self.headers.items()})
                if body:
                    try:
                        state.last_body = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        state.last_multipart = body

            # ---- 百炼：提交任务 ----
            def do_POST(self):
                body = self._read()
                if self.path.endswith("/services/audio/asr/transcription"):
                    state.counts["submit"] += 1
                    self._record(body)
                    if state.counts["submit"] <= state.flaky_submit:
                        return self._send(500, {"code": "InternalError"})
                    if state.no_task_id:
                        return self._send(200, {"output": {}, "request_id": "r"})
                    return self._send(200, {"output": {"task_id": "task-1"},
                                            "request_id": "r"})
                if self.path.endswith("/audio/transcriptions"):     # 硅基流动
                    state.counts["sf"] += 1
                    self._record(body)
                    if state.sf_segments is not None:
                        return self._send(200, {"text": "x",
                                                "segments": state.sf_segments})
                    return self._send(200, {"text": state.sf_text or "第一句。第二句！"})
                self._send(404, {"code": "NoSuchPath"})

            # ---- 百炼：查询任务 / 取结果 ----
            def do_GET(self):
                # 注意客户端会带上 base 路径前缀（例如 /api/v1/tasks/xxx），
                # 所以用包含判断而不是 startswith
                if "/tasks/" in self.path:
                    state.counts["poll"] += 1
                    self._record(kind="get")
                    if state.counts["poll"] <= state.flaky_poll:
                        return self._send(500, {"code": "InternalError"})
                    if state.task_fail:
                        return self._send(200, {"output": {"task_status": "FAILED",
                                                           "message": "boom"}})
                    if state.counts["poll"] <= state.polls_before_ready:
                        return self._send(200, {"output": {"task_status": "RUNNING"}})
                    if state.subtask_fail:
                        result = {"subtask_status": "FAILED", "message": "bad audio"}
                    elif state.missing_url:
                        result = {"subtask_status": "SUCCEEDED"}
                    else:
                        result = {"subtask_status": "SUCCEEDED",
                                  "transcription_url": f"{base}/result.json"}
                    return self._send(200, {"output": {"task_status": "SUCCEEDED",
                                                       "results": [result]}})
                if "/result.json" in self.path:
                    state.counts["result"] += 1
                    return self._send(200, state.result)
                self._send(404, {"code": "NoSuchPath"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        self.base = base
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return base + "/api/v1"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
