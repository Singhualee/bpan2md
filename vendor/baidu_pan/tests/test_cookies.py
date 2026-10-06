"""Offline tests for cookie decryption — fixtures encrypted via openssl CLI."""

import shutil
import subprocess

import pytest
from bdpan_cookies import decrypt_value, derive_key

KEY = derive_key("test-password")

openssl = shutil.which("openssl")
needs_openssl = pytest.mark.skipif(openssl is None, reason="openssl CLI not available")


def _encrypt(plaintext: bytes) -> bytes:
    """AES-128-CBC with IV=16 spaces, PKCS7 padding — same as Chrome."""
    proc = subprocess.run(
        [openssl, "enc", "-aes-128-cbc", "-K", KEY.hex(),
         "-iv", (b" " * 16).hex()],
        input=plaintext, capture_output=True, check=True,
    )
    return b"v10" + proc.stdout


@needs_openssl
def test_decrypt_roundtrip():
    secret = "lBQi1ZcDlPU1A5bWFIUUlJdnRBTVRWLXVRYlpZaH"
    assert decrypt_value(_encrypt(secret.encode()), KEY) == secret


@needs_openssl
def test_decrypt_strips_32_byte_domain_hash_prefix():
    # newer Chrome: plaintext = 32-byte SHA256(domain) hash + value
    prefix = bytes(range(32))  # all < 0x20 → detected as binary prefix
    payload = prefix + b"real-cookie-value"
    assert decrypt_value(_encrypt(payload), KEY) == "real-cookie-value"


@needs_openssl
def test_decrypt_keeps_printable_32_byte_prefix():
    # a value that legitimately starts with 32 printable chars must NOT be stripped
    payload = b"A" * 32 + b"-tail"
    assert decrypt_value(_encrypt(payload), KEY) == payload.decode()


def test_decrypt_plain_value_passthrough():
    # values without the v10 marker are stored in plain (legacy rows)
    assert decrypt_value(b"legacy-plain", KEY) == "legacy-plain"


def test_derive_key_deterministic():
    assert derive_key("pw") == derive_key("pw")
    assert len(derive_key("pw")) == 16
    assert derive_key("pw") != derive_key("pw2")
