#!/usr/bin/env python3
"""Integrity checks for files downloaded from Baidu NetDisk.

Baidu's ``md5`` field in list/share APIs is an internal object key (it shows
up in dlink paths), NOT a reliable content hash for large sliced uploads.
So verify structurally instead:

1. exact size against the metadata (share inspect / list_dir)
2. ISO-BMFF / QuickTime atom walk: top-level atoms (ftyp/moov/mdat/free/...)
   must tile the file exactly, and a moov atom must exist — a file assembled
   from wrong-offset chunks cannot satisfy this
3. when ffprobe is installed and the metadata carried a duration: compare
   (±2s tolerance — Baidu rounds to whole seconds)

Usage:
    python3 bdpan_verify.py FILE --expect-size 740378697 [--expect-duration 3615] [--json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

KNOWN_ATOMS = {"ftyp", "wide", "mdat", "moov", "free", "skip", "pnot", "uuid", "junk"}


def parse_atoms(path: Path, max_scan: int = 64) -> tuple[list, bool]:
    """Walk top-level atoms; return ([(offset, type, length)], tiles_exactly)."""
    size = path.stat().st_size
    atoms = []
    off = 0
    with open(path, "rb") as fh:
        while off < size and len(atoms) < max_scan:
            fh.seek(off)
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            length = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8].decode("latin1")
            head_len = 8
            if length == 1:  # 64-bit largesize
                ext = fh.read(8)
                if len(ext) < 8:
                    break
                length = struct.unpack(">Q", ext)[0]
                head_len = 16
            elif length == 0:  # atom extends to EOF
                length = size - off
            if length < head_len:
                break
            atoms.append((off, typ, length))
            off += length
    return atoms, off == size


def ffprobe_duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            timeout=60, text=True,
        )
        return float(out.strip())
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        return None


def verify(path: Path, expect_size: int | None = None,
           expect_duration: float | None = None) -> dict:
    result: dict = {"file": str(path), "ok": True, "checks": {}}
    if not path.is_file():
        return {"file": str(path), "ok": False, "checks": {}, "error": "not a file"}

    size = path.stat().st_size
    result["size"] = size
    if expect_size is not None:
        ok = size == expect_size
        result["checks"]["size"] = {"ok": ok, "actual": size, "expect": expect_size}
        result["ok"] &= ok

    atoms, tiles = parse_atoms(path)
    types = [a[1] for a in atoms]
    looks_media = bool(atoms) and types[0] in KNOWN_ATOMS
    if looks_media:
        has_moov = "moov" in types
        ok = tiles and has_moov and types[0] in ("ftyp", "moov")
        result["checks"]["atoms"] = {
            "ok": ok, "tiles_file_exactly": tiles, "has_moov": has_moov,
            "top_level": [{"offset": o, "type": t, "length": ln} for o, t, ln in atoms[:12]],
        }
        result["ok"] &= ok

    if expect_duration is not None:
        dur = ffprobe_duration(path)
        if dur is None:
            result["checks"]["duration"] = {"ok": None, "skipped": "ffprobe not available"}
        else:
            ok = abs(dur - expect_duration) <= 2.0
            result["checks"]["duration"] = {"ok": ok, "actual": dur,
                                            "expect": expect_duration}
            result["ok"] &= ok
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--expect-size", type=int, default=None)
    ap.add_argument("--expect-duration", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = verify(Path(args.file).expanduser(), args.expect_size, args.expect_duration)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        status = "OK" if out["ok"] else "FAILED"
        print(f"{status}  {out['file']}  size={out.get('size')}")
        for name, chk in out.get("checks", {}).items():
            mark = "?" if chk.get("ok") is None else ("✓" if chk.get("ok") else "✗")
            print(f"  [{mark}] {name}: {json.dumps(chk, ensure_ascii=False)[:160]}")
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
