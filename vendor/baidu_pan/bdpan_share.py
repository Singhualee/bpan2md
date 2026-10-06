#!/usr/bin/env python3
"""Inspect and transfer-save Baidu NetDisk share links.

Subcommands:
    inspect URL   verify the 提取码 and list the shared files (JSON out)
    save URL      transfer selected (or all) shared files into your own pan

The share page embeds a locals.mset({...}) JSON blob with everything needed:
shareid, share_uk and file_list (fs_id / size / md5 / duration).

Usage:
    python3 bdpan_share.py inspect "https://pan.baidu.com/s/1xxx?pwd=ab12" --cookies cookies.json
    python3 bdpan_share.py save    "https://pan.baidu.com/s/1xxx?pwd=ab12" \
        --cookies cookies.json --dest "/备份中转" [--fsids 123,456]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bdpan_common import (  # noqa: E402
    BaiduError,
    Session,
    check_cookies,
    human_size,
    load_cookies,
    parse_share_url,
)


def _reason_of(out: dict) -> str:
    """从接口返回里取出人类可读的原因。

    百度把这个放在 show_msg / errmsg / err_msg，而 `info` 经常是**空的**。
    上游只读 info，于是「文件已存在」被显示成一句毫无信息量的 `[]`。
    """
    for key in ("show_msg", "errmsg", "err_msg"):
        value = out.get(key)
        if value:
            return str(value)
    info = out.get("info")
    if info:
        return json.dumps(info, ensure_ascii=False)
    return ""


# 百度按内容去重：分享里的文件早就在你网盘里时，再转会返回这些文案。
_ALREADY_IN_PAN = ("文件已存在", "已存在", "already exists", "file exists")


def _already_in_pan(out: dict) -> bool:
    text = _reason_of(out) + " " + json.dumps(out.get("info") or [], ensure_ascii=False)
    return any(mark in text for mark in _ALREADY_IN_PAN)


def _session(args: argparse.Namespace) -> Session:
    cookies = load_cookies(args.cookies)
    missing = check_cookies(cookies)
    if missing:
        raise SystemExit(f"cookies.json missing {missing} — re-run bdpan_cookies.py")
    return Session(cookies)


def _share_files(data: dict) -> list[dict]:
    files = []
    for f in data.get("file_list", []):
        files.append({
            "fs_id": f.get("fs_id"),
            "name": f.get("server_filename"),
            "path": f.get("path"),
            "size": int(f.get("size", 0)),
            "isdir": int(f.get("isdir", 0)) == 1,
            # NOTE: this "md5" is Baidu's internal object key (it appears in
            # dlink paths) — NOT a content hash for large/sliced files.
            "md5_object_key": f.get("md5"),
            "duration": f.get("duration"),
        })
    return files


def cmd_inspect(args: argparse.Namespace) -> int:
    surl, url_pwd = parse_share_url(args.url)
    pwd = args.pwd or url_pwd
    if not pwd:
        raise SystemExit("no 提取码: pass --pwd or include ?pwd= in the URL")
    sess = _session(args)
    data = sess.fetch_share_page(surl, pwd)
    out = {
        "surl": surl,
        "shareid": data.get("shareid"),
        "share_uk": data.get("share_uk"),
        "share_user": data.get("linkusername"),
        "expired": data.get("expiredType") not in (0, None) and data.get("expiredType") != 544389,
        "count": len(data.get("file_list", [])),
        "files": _share_files(data),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if not args.json:
        for f in out["files"]:
            dur = f" dur={f['duration']}s" if f.get("duration") else ""
            print(f"  {'DIR ' if f['isdir'] else 'FILE'} {f['fs_id']} "
                  f"{human_size(f['size'])}{dur}  {f['name']}", file=sys.stderr)
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    surl, url_pwd = parse_share_url(args.url)
    pwd = args.pwd or url_pwd
    if not pwd:
        raise SystemExit("no 提取码: pass --pwd or include ?pwd= in the URL")
    sess = _session(args)
    data = sess.fetch_share_page(surl, pwd)
    shareid = data["shareid"]
    share_uk = data["share_uk"]
    files = _share_files(data)
    if args.fsids:
        wanted = {int(x) for x in args.fsids.split(",")}
        files = [f for f in files if f["fs_id"] in wanted]
        if not files:
            raise SystemExit(f"--fsids {sorted(wanted)} not in share (inspect first)")

    bdstoken = sess.bdstoken()

    # 先问再建：/api/create 对**已存在**的目录不报错，而是新建一个带时间戳的
    # 副本（`/x` → `/x_20261005_211106`）。反复跑同一批链接会在用户网盘里堆出
    # 一串看不出用途的目录。list_dir 对不存在的目录会抛错，正好拿来判断。
    try:
        sess.list_dir(args.dest, bdstoken)
        dest_exists = True
    except BaiduError:
        dest_exists = False
    if not dest_exists:
        sess.get_json("https://pan.baidu.com/api/create",
                      params={"a": "commit", "bdstoken": bdstoken,
                              "channel": "chunlei", "web": "1", "clienttype": "0"},
                      data={"path": args.dest, "isdir": "1", "size": "0",
                            "block_list": "[]"})

    fsidlist = json.dumps([f["fs_id"] for f in files])
    out = sess.get_json("https://pan.baidu.com/share/transfer",
                        params={"shareid": shareid, "from": share_uk,
                                "bdstoken": bdstoken, "ondup": "newcopy", "async": "2",
                                "channel": "chunlei", "web": "1", "clienttype": "0"},
                        data={"fsidlist": fsidlist, "path": args.dest})
    if out.get("errno") != 0:
        if _already_in_pan(out):
            # **这不是失败。** 百度转存接口按内容去重：这份分享的内容早就在你
            # 网盘里了（你自己存过，或上一轮已经转过），所以它拒绝再存一份。
            # 继续往下走，靠下面的兜底逻辑把已有的那份找出来直接用。
            print(f"note: already in your pan ({_reason_of(out)}) - reusing it",
                  file=sys.stderr)
        else:
            raise BaiduError(out.get("errno"), _reason_of(out))

    # Map each selected share item by source fs_id for metadata lookup
    by_fsid = {f["fs_id"]: f for f in files}
    saved: list[dict] = []
    extra_list = (out.get("extra") or {}).get("list") or []
    if extra_list:
        for item in extra_list:
            src = by_fsid.get(item.get("from_fs_id"), {})
            to_path = item.get("to") or f"{args.dest}/{src.get('name', '?')}"
            if src.get("isdir"):
                # folder share: walk the transferred subtree; relpath keeps the
                # folder name as root so `download --batch` mirrors the tree
                for f in sess.list_tree(to_path, bdstoken):
                    f["duration"] = None
                    saved.append(f)
            else:
                saved.append({
                    "fs_id": item.get("to_fs_id"),
                    "path": to_path,
                    "name": to_path.rsplit("/", 1)[-1],
                    "relpath": to_path.rsplit("/", 1)[-1],
                    "size": int(src.get("size", 0)),
                    "duration": src.get("duration"),
                })
    else:
        # async transfer without an immediate mapping: fall back to matching
        # the destination listing by name (files only; folders need list_tree)
        expected = {f["name"] for f in files}
        listing: list[dict] = []
        for _ in range(30):  # up to ~60s for the async task to land
            listing = sess.list_dir(args.dest, bdstoken)
            found = {i.get("server_filename") for i in listing}
            if expected <= found:
                break
            time.sleep(2)

        found = {i.get("server_filename") for i in listing}
        if not expected <= found:
            # 目标目录里没有。最常见的解释是：这份内容早就在网盘别处了，
            # 百度按内容去重、拒绝再存一份（见上面的「文件已存在」）。
            # 那就全局按名字找出来——对下载来说和刚转存完完全等价。
            for name in sorted(expected - found):
                for hit in sess.search(name, limit=10):
                    if hit.get("server_filename") == name:
                        listing.append(hit)

        for item in listing:
            name = item.get("server_filename")
            src = next((f for f in files if f["name"] == name), None)
            if src is None:
                continue
            if str(item.get("isdir", 0)) == "1":
                for f in sess.list_tree(item["path"], bdstoken):
                    f["duration"] = None
                    saved.append(f)
            else:
                saved.append({"fs_id": item["fs_id"], "path": item["path"],
                              "name": name, "relpath": name,
                              "size": int(item.get("size", 0)),
                              "duration": src.get("duration")})

    print(json.dumps({"dest": args.dest, "saved": saved}, ensure_ascii=False, indent=1))
    n_dirs = sum(1 for f in files if f["isdir"])
    if len(saved) < len(files) - n_dirs:
        print(f"warning: selected {len(files)} items but collected {len(saved)} "
              f"files under {args.dest}", file=sys.stderr)
    return 0


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cookies", default="cookies.json")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_in = sub.add_parser("inspect", help="list shared files (JSON to stdout)",
                          parents=[common])
    p_in.add_argument("url")
    p_in.add_argument("--pwd", default=None)
    p_in.add_argument("--json", action="store_true", help="suppress the human summary on stderr")
    p_in.set_defaults(fn=cmd_inspect)

    p_sv = sub.add_parser("save", help="transfer shared files into your own pan",
                          parents=[common])
    p_sv.add_argument("url")
    p_sv.add_argument("--pwd", default=None)
    p_sv.add_argument("--dest", required=True, help="destination dir in your pan, e.g. /备份中转")
    p_sv.add_argument("--fsids", default=None, help="comma-separated fs_id subset (default: all)")
    p_sv.set_defaults(fn=cmd_save)

    args = ap.parse_args()
    try:
        raise SystemExit(args.fn(args))
    except BaiduError as e:
        raise SystemExit(f"baidu api error: {e}") from e


if __name__ == "__main__":
    main()
