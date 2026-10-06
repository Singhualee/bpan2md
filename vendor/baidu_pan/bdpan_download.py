#!/usr/bin/env python3
"""Resumable downloader for files in your own Baidu NetDisk.

Uses the legacy PCS endpoint, which — unlike ``api/filemetas`` dlinks — does
NOT fail with the dreaded ``31362 sign error``:

    GET https://d.pcs.baidu.com/rest/2.0/pcs/file?method=download
        &app_id=250528&path=<urlencoded remote path>
    headers: User-Agent: pan.baidu.com, Cookie: BDUSS=...; ..., Range: bytes=a-b

Speed reality check: throttling is ACCOUNT-WIDE (~50 KB/s aggregated for
non-SVIP; 4/8/16 parallel range requests do not add up). This tool therefore
downloads sequentially and focuses on being unkillable: 4 MB chunks, a
``*.dlstate.json`` resume ledger, per-chunk retries with capped backoff, and —
on macOS — automatic cookie re-extraction when the session expires mid-run.
Plan multi-GB jobs in hours, or use an SVIP account.

Integrity: every chunk's Content-Range is checked against the requested
window (a CDN serving the wrong slice fails loudly instead of corrupting the
file). Baidu's ``md5`` list field is an object key, NOT a content hash — use
``--verify`` for size + container-atom + duration verification after download.

Usage:
    python3 bdpan_download.py --cookies cookies.json \
        --remote "/备份中转/视频.mov" [--size 740378697] [--out DIR] [--verify]
    python3 bdpan_download.py --cookies cookies.json --batch saved.json --out DIR --verify
        # saved.json = output of `bdpan_share.py save`
        # ({saved:[{path,size,relpath?,duration?}]}; relpath mirrors folder shares)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bdpan_common import (  # noqa: E402
    DOWNLOAD_UA,
    AuthExpiredError,
    filter_cookies,
    is_auth_error,
    load_cookies,
)

PCS_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/file"
CHUNK = 4 * 1024 * 1024
MAX_RETRY = 200
MAX_AUTH_REFRESH = 2
LOW_SPEED_BPS = 100 * 1024       # below this: show the account-throttle advisory
LOW_SPEED_AFTER_CHUNKS = 5       # ...once this many chunks landed in this session

Fetcher = Callable[[str, int, int], bytes]


def _pcs_url(remote: str) -> str:
    return PCS_URL + "?method=download&app_id=250528&path=" + urllib.parse.quote(remote)


def _range_request(remote: str, cookie_header: str, start: int, end: int,
                   timeout: int) -> tuple:
    """One ranged GET; returns (status, content_range_header, body)."""
    req = urllib.request.Request(_pcs_url(remote), method="GET")
    req.add_header("User-Agent", DOWNLOAD_UA)
    req.add_header("Cookie", cookie_header)
    req.add_header("Range", f"bytes={start}-{end}")
    req.add_header("Accept-Encoding", "identity")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Range", ""), resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        if is_auth_error(e.code, body):
            raise AuthExpiredError(f"http-{e.code}", body) from e
        raise OSError(f"HTTP {e.code} for range {start}-{end}: {body}") from e


def check_content_range(header: str, start: int, end: int) -> bool:
    """True when a Content-Range header matches the requested window.

    An empty header is tolerated (some edges omit it — the caller's length
    check still guards those), but a PRESENT header must match exactly.
    """
    if not header:
        return True
    m = header.strip()
    if m.lower().startswith("bytes "):
        m = m[6:]
    window = m.split("/", 1)[0]
    parts = window.split("-", 1)
    if len(parts) != 2:
        return False
    try:
        got_start, got_end = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return got_start == start and got_end == end


def make_fetcher(cookies: dict, timeout: int = 120) -> Fetcher:
    """Build a fetch closure over a MUTABLE cookie dict.

    The dict is captured by reference: refreshing cookies in place (clear +
    update) immediately affects subsequent fetches — used by the automatic
    credential-recovery hook.
    """

    def fetch(remote: str, start: int, end: int) -> bytes:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        status, cr, data = _range_request(remote, cookie_header, start, end, timeout)
        if status not in (200, 206):
            raise OSError(f"HTTP {status} for range {start}-{end}")
        if not check_content_range(cr, start, end):
            raise OSError(f"Content-Range mismatch: requested {start}-{end}, got {cr!r}")
        return data

    return fetch


def probe_size(cookies: dict, remote: str, attempts: int = 3) -> int:
    """GET the first byte and read the total from Content-Range.

    Retries a few times: Baidu's auth backend occasionally returns transient
    403/31045 even with perfectly valid cookies.
    """
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    cr = ""
    last_err = ""
    for attempt in range(attempts):
        req = urllib.request.Request(_pcs_url(remote), method="GET")
        req.add_header("User-Agent", DOWNLOAD_UA)
        req.add_header("Cookie", cookie_header)
        req.add_header("Range", "bytes=0-0")
        req.add_header("Accept-Encoding", "identity")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                cr = resp.headers.get("Content-Range", "")
        except urllib.error.HTTPError as e:
            cr = e.headers.get("Content-Range", "") if e.headers else ""
            if e.code == 403:
                last_err = e.read()[:200].decode("utf-8", "replace")
        except OSError as e:
            last_err = str(e)[:200]
        if cr:
            break
        time.sleep(2 + attempt * 2)
    else:
        if last_err:
            raise SystemExit(
                f"PCS endpoint rejected auth after {attempts} tries: {last_err}\n"
                "cookies expired/insufficient — re-run bdpan_cookies.py")
    m = cr.rsplit("/", 1)
    if len(m) != 2 or not m[1].isdigit():
        raise SystemExit(f"cannot probe size of {remote!r} (Content-Range={cr!r}) — pass --size")
    return int(m[1])


def plan_chunks(size: int, chunk: int = CHUNK) -> int:
    return (size + chunk - 1) // chunk


def load_state(state_path: Path, total_chunks: int, size: int) -> set:
    if state_path.exists():
        try:
            st = json.loads(state_path.read_text())
            if st.get("total_chunks") == total_chunks and st.get("size") == size:
                return set(st.get("done", []))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_state(state_path: Path, done: set, total_chunks: int, size: int) -> None:
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done), "total_chunks": total_chunks,
                               "size": size}))
    os.replace(tmp, state_path)


def download_one(fetch: Fetcher, remote: str, out_dir: Path, name: str,
                 size: int | None = None, chunk: int = CHUNK,
                 cookies: dict | None = None, relpath: str | None = None,
                 on_auth_error: Callable[[], bool] | None = None,
                 log: Callable[[str], None] = print) -> Path:
    """Download one remote file with resume; returns the local path.

    ``relpath`` mirrors folder-share structure (subdirectories are created).
    ``on_auth_error`` is invoked when credentials die mid-run; when it returns
    True the same chunk is retried with refreshed cookies (bounded by
    MAX_AUTH_REFRESH per run).
    """
    local = out_dir / (relpath or name)
    local.parent.mkdir(parents=True, exist_ok=True)
    state_path = Path(str(local) + ".dlstate.json")
    if size is None:
        if not cookies:
            raise ValueError("pass size= or cookies= (needed to probe the remote size)")
        size = probe_size(cookies, remote)
    total_chunks = plan_chunks(size, chunk)
    done = load_state(state_path, total_chunks, size)

    if not local.exists() or local.stat().st_size != size:
        with open(local, "wb") as fh:      # preallocate (sparse on APFS/ext4)
            fh.truncate(size)
    if len(done) >= total_chunks and local.stat().st_size == size:
        log(f"[{name}] already complete ({size} B)")
        return local

    log(f"[{name}] resuming at {len(done)}/{total_chunks} chunks ({size} B total)")
    t0 = time.time()
    session_bytes = 0        # bytes fetched THIS run — resumed chunks don't count
    auth_refreshes = 0
    advised = False
    with open(local, "r+b") as fh:
        i = 0
        while i < total_chunks:
            if i in done:
                i += 1
                continue
            start = i * chunk
            end = min(start + chunk - 1, size - 1)
            expect = end - start + 1
            ok = False
            for attempt in range(MAX_RETRY):
                try:
                    data = fetch(remote, start, end)
                    if len(data) == expect:
                        fh.seek(start)
                        fh.write(data)
                        fh.flush()
                        done.add(i)
                        session_bytes += expect
                        if len(done) % 10 == 0 or len(done) == total_chunks:
                            save_state(state_path, done, total_chunks, size)
                            el = time.time() - t0
                            spd = session_bytes / el if el else 0
                            remaining = (total_chunks - len(done)) * chunk
                            eta_h = remaining / max(spd, 1) / 3600
                            log(f"[{name}] {len(done)}/{total_chunks} "
                                f"({100*len(done)/total_chunks:.1f}%) "
                                f"~{spd/1024:.0f}KB/s eta {eta_h:.1f}h")
                            if (not advised and 0 < spd < LOW_SPEED_BPS
                                    and session_bytes >= LOW_SPEED_AFTER_CHUNKS * chunk):
                                advised = True
                                log(f"[{name}] NOTE: sustained ~{spd/1024:.0f}KB/s — this is "
                                    "Baidu's ACCOUNT-WIDE throttle for non-SVIP accounts; "
                                    "parallel connections will NOT help. Only SVIP "
                                    "speeds this up.")
                        ok = True
                        break
                    log(f"[{name}] chunk {i}: short read {len(data)}/{expect}, retry")
                except AuthExpiredError as e:
                    if on_auth_error is not None and auth_refreshes < MAX_AUTH_REFRESH:
                        auth_refreshes += 1
                        log(f"[{name}] chunk {i}: credentials expired ({e}); "
                            f"auto-refreshing cookies ({auth_refreshes}/{MAX_AUTH_REFRESH})")
                        if on_auth_error():
                            continue  # retry same chunk; attempt budget intact
                        log(f"[{name}] cookie refresh failed")
                    save_state(state_path, done, total_chunks, size)
                    raise OSError(
                        f"credentials rejected ({e}) — re-run bdpan_cookies.py "
                        "(or pass a fresh cookies.json); progress saved") from e
                except KeyboardInterrupt:
                    save_state(state_path, done, total_chunks, size)
                    log(f"[{name}] interrupted — state saved at {len(done)}/{total_chunks}")
                    raise
                except Exception as e:  # network hiccups, transient errors, timeouts
                    log(f"[{name}] chunk {i}: {type(e).__name__} {str(e)[:80]}")
                time.sleep(min(30, 2 + attempt * 2))
            if not ok:
                save_state(state_path, done, total_chunks, size)
                raise OSError(f"gave up on chunk {i} after {MAX_RETRY} attempts")
            i += 1
    save_state(state_path, done, total_chunks, size)
    actual = local.stat().st_size
    if actual != size:
        raise OSError(f"size mismatch: {actual} != {size}")
    log(f"[{name}] DONE {size} B in {(time.time()-t0)/60:.1f} min")
    return local


def make_auth_refresher(cookies: dict, cookies_file: Path | None, browser: str,
                        log: Callable[[str], None] = print):
    """Build the on_auth_error hook: re-extract cookies from the browser
    (macOS only) and update *cookies* in place; also refresh cookies.json."""
    if sys.platform != "darwin":
        return None

    def refresh() -> bool:
        try:
            from bdpan_cookies import extract_cookies
            fresh = filter_cookies(extract_cookies(browser))
            if not fresh.get("BDUSS"):
                log("cookie refresh produced no BDUSS — browser logged out?")
                return False
            cookies.clear()
            cookies.update(fresh)
            if cookies_file is not None:
                cookies_file.write_text(json.dumps(fresh, ensure_ascii=False, indent=1))
                os.chmod(cookies_file, 0o600)
            log("cookie refresh OK — resuming")
            return True
        except Exception as e:
            log(f"cookie refresh failed: {type(e).__name__} {str(e)[:100]}")
            return False

    return refresh


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cookies", default="cookies.json")
    ap.add_argument("--remote", help="absolute path inside your pan")
    ap.add_argument("--size", type=int, default=None, help="skip probing when known")
    ap.add_argument("--name", default=None, help="local file name (default: remote basename)")
    ap.add_argument("--batch", help="JSON with {saved:[{path,size,relpath?,duration?}]} "
                                    "(bdpan_share.py save output)")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--chunk-mb", type=int, default=4)
    ap.add_argument("--verify", action="store_true",
                    help="after each file: size + container-atom + duration checks "
                         "(duration when the batch carries it)")
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge", "brave"],
                    help="browser for automatic cookie refresh on auth expiry (macOS)")
    args = ap.parse_args()

    if bool(args.remote) == bool(args.batch):
        raise SystemExit("pass exactly one of --remote / --batch")

    cookies_path = Path(args.cookies).expanduser()
    cookies = load_cookies(cookies_path)
    refresh = make_auth_refresher(cookies, cookies_path, args.browser)
    fetch = make_fetcher(cookies)
    out_dir = Path(args.out).expanduser()
    chunk = args.chunk_mb * 1024 * 1024

    jobs = []
    if args.remote:
        name = args.name or Path(args.remote).name
        jobs.append({"path": args.remote, "name": name, "size": args.size})
    else:
        data = json.loads(Path(args.batch).read_text())
        for item in data.get("saved", []):
            jobs.append(item)

    failures = 0
    for job in jobs:
        remote = job["path"]
        name = job.get("name") or Path(remote).name
        try:
            local = download_one(fetch, remote, out_dir, name,
                                 size=job.get("size"), chunk=chunk,
                                 cookies=cookies, relpath=job.get("relpath"),
                                 on_auth_error=refresh)
            if args.verify:
                from bdpan_verify import verify as verify_file
                v = verify_file(local, expect_size=job.get("size"),
                                expect_duration=job.get("duration"))
                if v["ok"]:
                    print(f"VERIFY OK  {name}")
                else:
                    print(f"VERIFY FAILED  {name}: "
                          f"{json.dumps(v.get('checks', {}), ensure_ascii=False)[:200]}",
                          file=sys.stderr)
                    failures += 1
        except (OSError, ValueError, SystemExit) as e:
            print(f"FAILED {remote}: {e}", file=sys.stderr)
            failures += 1
    total = len(jobs)
    if failures:
        print(f"{failures}/{total} FAILED — re-run to resume", file=sys.stderr)
    else:
        print(f"ALL_DONE ({total}/{total} ok{', verified' if args.verify else ''})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
