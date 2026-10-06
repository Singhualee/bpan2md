"""Offline tests for the resumable downloader — fake fetcher, no network."""

import json

import bdpan_download as dl
import pytest


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(dl, "MAX_RETRY", 2)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)


def make_content(n: int) -> bytes:
    return bytes((i * 7 + 13) % 256 for i in range(n))


def good_fetcher(content: bytes, calls: list):
    def fetch(remote, start, end):
        calls.append((start, end))
        return content[start:end + 1]
    return fetch


def test_plan_chunks():
    assert dl.plan_chunks(10, 4) == 3
    assert dl.plan_chunks(12, 4) == 3
    assert dl.plan_chunks(0, 4) == 0


def test_state_roundtrip(tmp_path):
    sp = tmp_path / "f.dlstate.json"
    dl.save_state(sp, {0, 2}, 5, 100)
    assert dl.load_state(sp, 5, 100) == {0, 2}
    # mismatched geometry is discarded (stale ledger from another file)
    assert dl.load_state(sp, 6, 100) == set()
    assert dl.load_state(sp, 5, 101) == set()


def test_download_full(tmp_path):
    content = make_content(10000)
    calls: list = []
    out = dl.download_one(good_fetcher(content, calls), "/pan/f.bin",
                          tmp_path, "f.bin", size=len(content), chunk=1024,
                          log=lambda m: None)
    assert out.read_bytes() == content
    assert len(calls) == 10  # 10000 / 1024 → 10 chunks
    state = json.loads((tmp_path / "f.bin.dlstate.json").read_text())
    assert len(state["done"]) == 10


def test_download_resumes_after_failure(tmp_path):
    content = make_content(5000)

    def failing(remote, start, end):
        if start == 2048:  # chunk index 2 always fails
            raise OSError("simulated network death")
        return content[start:end + 1]

    with pytest.raises(IOError):
        dl.download_one(failing, "/pan/f.bin", tmp_path, "f.bin",
                        size=len(content), chunk=1024, log=lambda m: None)

    # chunks 0,1 landed in the ledger; 2 failed, rest never attempted
    state = json.loads((tmp_path / "f.bin.dlstate.json").read_text())
    assert sorted(state["done"]) == [0, 1]

    calls: list = []
    out = dl.download_one(good_fetcher(content, calls), "/pan/f.bin",
                          tmp_path, "f.bin", size=len(content), chunk=1024,
                          log=lambda m: None)
    assert out.read_bytes() == content
    starts = {s for s, _ in calls}
    assert 0 not in starts and 1024 not in starts  # completed chunks NOT re-fetched
    assert 2048 in starts


def test_download_short_read_retried(tmp_path):
    content = make_content(2000)
    attempts = {"n": 0}

    def flaky(remote, start, end):
        if start == 0 and attempts["n"] == 0:
            attempts["n"] += 1
            return content[start:start + 100]  # short read
        return content[start:end + 1]

    out = dl.download_one(flaky, "/pan/f.bin", tmp_path, "f.bin",
                          size=len(content), chunk=1024, log=lambda m: None)
    assert out.read_bytes() == content
    assert attempts["n"] == 1


# --- v0.2: Content-Range validation, auth refresh, relpath ---

from bdpan_common import AuthExpiredError  # noqa: E402
from bdpan_download import check_content_range  # noqa: E402


@pytest.mark.parametrize("hdr,start,end,ok", [
    ("bytes 0-1023/5000", 0, 1023, True),
    ("bytes 1024-2047/5000", 1024, 2047, True),
    ("bytes 0-1023/5000", 1024, 2047, False),   # wrong window
    ("", 0, 1023, True),                         # absent header tolerated
    ("bytes */5000", 0, 0, False),               # malformed for a 206
    ("garbage", 0, 10, False),
])
def test_check_content_range(hdr, start, end, ok):
    assert check_content_range(hdr, start, end) is ok


def test_download_auth_refresh_recovers(tmp_path):
    content = make_content(3000)
    state = {"failed": True}
    refreshes = []

    def auth_then_good(remote, start, end):
        if state["failed"]:
            state["failed"] = False
            raise AuthExpiredError("http-403", '{"error_code":31045}')
        return content[start:end + 1]

    def hook():
        refreshes.append(1)
        return True

    out = dl.download_one(auth_then_good, "/pan/f.bin", tmp_path, "f.bin",
                          size=len(content), chunk=1024, on_auth_error=hook,
                          log=lambda m: None)
    assert out.read_bytes() == content
    assert len(refreshes) == 1


def test_download_auth_refresh_exhausted_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "MAX_AUTH_REFRESH", 2)
    monkeypatch.setattr(dl, "MAX_RETRY", 10)  # auth bound must trip first

    def always_auth_error(remote, start, end):
        raise AuthExpiredError("http-403", '{"error_code":31045}')

    refreshes = []
    with pytest.raises(OSError, match="credentials rejected"):
        dl.download_one(always_auth_error, "/pan/f.bin", tmp_path, "f.bin",
                        size=2048, chunk=1024,
                        on_auth_error=lambda: (refreshes.append(1), True)[1],
                        log=lambda m: None)
    assert len(refreshes) == 2  # bounded, not MAX_RETRY


def test_download_auth_error_without_hook_fails_fast(tmp_path):
    def always_auth_error(remote, start, end):
        raise AuthExpiredError("http-403", '{"error_code":31045}')

    with pytest.raises(OSError, match="credentials rejected"):
        dl.download_one(always_auth_error, "/pan/f.bin", tmp_path, "f.bin",
                        size=1024, chunk=1024, log=lambda m: None)


def test_download_relpath_creates_subdirs(tmp_path):
    content = make_content(2000)
    calls: list = []
    out = dl.download_one(good_fetcher(content, calls), "/pan/课程/x.bin",
                          tmp_path, "x.bin", size=len(content), chunk=1024,
                          relpath="课程/sub/x.bin", log=lambda m: None)
    assert out == tmp_path / "课程/sub/x.bin"
    assert out.read_bytes() == content
    assert (tmp_path / "课程/sub/x.bin.dlstate.json").exists()
