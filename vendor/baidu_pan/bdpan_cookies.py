#!/usr/bin/env python3
"""Extract logged-in pan.baidu.com cookies from a local Chromium browser.

macOS only (Keychain + Chrome Safe Storage). The browser must have an active
Baidu login (https://pan.baidu.com). Output is a 0600 JSON file mapping
cookie name -> value; duplicate names across hosts are resolved with
pan.baidu.com taking priority (important for STOKEN).

Decryption notes (learned the hard way):
- key = PBKDF2-HMAC-SHA1(password from Keychain "Chrome Safe Storage",
  salt=b"saltysalt", iterations=1003, dklen=16)
- values are prefixed b"v10", then AES-128-CBC with IV = 16 spaces
- NEWER Chrome prefixes the *plaintext* with a 32-byte SHA256 domain hash —
  when the first 32 decrypted bytes look binary, strip them
- AES is done via pycryptodome when importable, otherwise via the openssl CLI
  (both keep this script dependency-free for the common macOS case)

Usage:
    python3 bdpan_cookies.py [--browser chrome|edge] [--profile-dir DIR]
                             [--out cookies.json] [--quiet]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

KEYCHAIN_SERVICE = {
    "chrome": "Chrome Safe Storage",
    "edge": "Microsoft Edge Safe Storage",
    "brave": "Brave Safe Storage",
}
DEFAULT_COOKIE_DB = {
    "chrome": "~/Library/Application Support/Google/Chrome/Default/Cookies",
    "edge": "~/Library/Application Support/Microsoft Edge/Default/Cookies",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies",
}
# cookies that pan.baidu.com web APIs actually need
WANTED = [
    "BDUSS", "BDUSS_BFESS", "STOKEN", "PTOKEN", "BAIDUID", "BAIDUID_BFESS",
    "PSTM", "BIDUPSID", "H_PS_PSSID", "PSINO", "ndut_fmt", "PANWEB", "PANPSC",
]
REQUIRED = ["BDUSS", "STOKEN", "BAIDUID"]


def get_safe_storage_password(browser: str) -> str:
    service = KEYCHAIN_SERVICE[browser]
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SystemExit(
            f"cannot read Keychain item {service!r} ({e}).\n"
            "Is the browser installed on this Mac? You may need to allow "
            "access in the Keychain prompt."
        ) from e
    return out.strip()


def derive_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, dklen=16)


def _aes_cbc_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-CBC decrypt with IV=16 spaces; strip PKCS7 padding."""
    try:
        from Crypto.Cipher import AES  # type: ignore

        cipher = AES.new(key, AES.MODE_CBC, iv=b" " * 16)
        out = cipher.decrypt(data)
    except ImportError:
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(),
             "-iv", (b" " * 16).hex(), "-nopad"],
            input=data, capture_output=True, check=True,
        )
        out = proc.stdout
    pad = out[-1]
    if isinstance(pad, int) and 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        out = out[:-pad]
    return out


def decrypt_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt one Chrome cookie value; returns '' when undecryptable."""
    if encrypted[:3] in (b"v10", b"v11"):
        try:
            out = _aes_cbc_decrypt(encrypted[3:], key)
        except Exception:
            return ""
        # newer Chrome prepends a 32-byte SHA256(domain) hash to the plaintext
        if len(out) > 32 and any(b > 127 or b < 32 for b in out[:32]):
            out = out[32:]
        return out.decode("utf-8", errors="replace")
    return encrypted.decode("utf-8", errors="replace")


def extract(cookie_db: Path, key: bytes) -> dict[str, str]:
    """Read + decrypt baidu.com cookies; pan.baidu.com hosts win duplicates."""
    if not cookie_db.exists():
        raise SystemExit(f"cookie database not found: {cookie_db}")
    # Chrome locks its DB while running — work on a copy
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(cookie_db, tmp_path)
        conn = sqlite3.connect(str(tmp_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT host_key, name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%baidu.com%' ORDER BY "
            "CASE WHEN host_key LIKE '%pan.baidu.com%' THEN 1 ELSE 0 END"
        )
        rows = cur.fetchall()
        conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    cookies: dict[str, str] = {}
    for _host, name, enc in rows:
        val = decrypt_value(bytes(enc), key)
        if not val or not val.isascii():
            continue
        if name in cookies and len(val) < len(cookies[name]):
            continue  # keep the longer (pan-scoped values win via ORDER BY)
        cookies[name] = val
    return cookies


def extract_cookies(browser: str = "chrome", profile_dir: str | None = None) -> dict:
    """Extract + decrypt baidu.com cookies; reusable API for other scripts.

    macOS only. Raises SystemExit with guidance elsewhere.
    """
    if sys.platform != "darwin":
        raise SystemExit(
            "cookie extraction is macOS-only (Keychain). On Linux the Safe Storage "
            "password is the fixed string 'peanuts'; Windows needs DPAPI — see "
            "issues #1/#2. Bring your own cookies.json on other platforms."
        )
    if profile_dir:
        db = Path(profile_dir).expanduser() / "Cookies"
    else:
        db = Path(DEFAULT_COOKIE_DB[browser]).expanduser()
    password = get_safe_storage_password(browser)
    return extract(db, derive_key(password))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--browser", choices=sorted(KEYCHAIN_SERVICE), default="chrome")
    ap.add_argument("--profile-dir", default=None,
                    help="directory containing the Cookies DB (default: browser Default profile)")
    ap.add_argument("--out", default="cookies.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cookies = extract_cookies(args.browser, args.profile_dir)

    out = Path(args.out)
    out.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(out, 0o600)

    missing = [k for k in REQUIRED if not cookies.get(k)]
    if not args.quiet:
        have = [k for k in WANTED if cookies.get(k)]
        print(f"wrote {out} ({len(cookies)} cookies; key ones: {', '.join(have) or 'NONE'})")
    if missing:
        raise SystemExit(
            f"missing required cookies: {', '.join(missing)} — "
            "log in to https://pan.baidu.com in the browser first, then re-run"
        )
    if not args.quiet:
        print("OK — BDUSS/STOKEN/BAIDUID present")


if __name__ == "__main__":
    main()
