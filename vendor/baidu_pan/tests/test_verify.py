"""Offline tests for bdpan_verify — synthetic QuickTime/ISO-BMFF containers."""

import struct

from bdpan_verify import parse_atoms, verify


def atom(typ: str, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ.encode() + payload


def make_mov(tmp_path, name="v.mov", with_moov=True, truncate=0):
    body = (
        atom("ftyp", b"qt  " + b"\x00" * 8)
        + atom("wide", b"")
        + atom("mdat", b"\xab" * 500)
        + (atom("moov", b"\x00" * 40) if with_moov else b"")
    )
    if truncate:
        body = body[:-truncate]
    p = tmp_path / name
    p.write_bytes(body)
    return p, len(body)


def test_parse_atoms_tiles_exactly(tmp_path):
    p, size = make_mov(tmp_path)
    atoms, tiles = parse_atoms(p)
    assert tiles
    assert [t for _, t, _ in atoms] == ["ftyp", "wide", "mdat", "moov"]
    assert sum(ln for _, _, ln in atoms) == size


def test_verify_ok(tmp_path):
    p, size = make_mov(tmp_path)
    r = verify(p, expect_size=size)
    assert r["ok"], r
    assert r["checks"]["size"]["ok"]
    assert r["checks"]["atoms"]["ok"]


def test_verify_size_mismatch(tmp_path):
    p, size = make_mov(tmp_path)
    r = verify(p, expect_size=size + 1)
    assert not r["ok"]
    assert not r["checks"]["size"]["ok"]


def test_verify_truncated_file_fails_atom_tiling(tmp_path):
    p, size = make_mov(tmp_path, truncate=10)
    r = verify(p, expect_size=size - 10)
    assert not r["ok"]
    assert not r["checks"]["atoms"]["tiles_file_exactly"]


def test_verify_missing_moov_fails(tmp_path):
    p, size = make_mov(tmp_path, with_moov=False)
    r = verify(p, expect_size=size)
    assert not r["ok"]
    assert not r["checks"]["atoms"]["has_moov"]


def test_verify_non_media_size_only(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"hello world")
    r = verify(p, expect_size=11)
    assert r["ok"]
    assert "atoms" not in r["checks"]  # atom check skipped for non-containers


def test_verify_missing_file():
    from pathlib import Path

    r = verify(Path("/nonexistent/x.mov"))
    assert not r["ok"]
    assert r["error"] == "not a file"
