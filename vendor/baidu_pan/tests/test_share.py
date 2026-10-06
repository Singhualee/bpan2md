"""Offline tests for bdpan_share save-flow (folder recursion + metadata)."""

import json
from argparse import Namespace

import bdpan_share
import pytest
from bdpan_share import _share_files, cmd_save

SHARE_DATA = {
    "shareid": 1, "share_uk": 2, "linkusername": "u", "expiredType": 0,
    "file_list": [
        {"fs_id": 111, "server_filename": "课程", "path": "/课程", "size": 0,
         "isdir": 1, "md5": None, "duration": None, "category": 0},
        {"fs_id": 333, "server_filename": "a.mov", "path": "/a.mov", "size": 100,
         "isdir": 0, "md5": "objkey", "duration": 55, "category": 1},
    ],
}


class FakeSession:
    def __init__(self, transfer_out=None):
        self._transfer_out = transfer_out if transfer_out is not None else {
            "errno": 0,
            "extra": {"list": [
                {"from": "/课程", "from_fs_id": 111, "to": "/dest/课程", "to_fs_id": 222},
                {"from": "/a.mov", "from_fs_id": 333, "to": "/dest/a.mov", "to_fs_id": 444},
            ]},
        }
        self.tree_calls = []

    def fetch_share_page(self, surl, pwd):
        return SHARE_DATA

    def bdstoken(self):
        return "tok"

    def get_json(self, url, params=None, data=None):
        if "share/transfer" in url:
            return self._transfer_out
        return {"errno": 0}

    def list_tree(self, root, tok):
        self.tree_calls.append(root)
        return [{"fs_id": 9, "path": f"{root}/x.mov", "relpath": "课程/x.mov",
                 "name": "x.mov", "size": 50}]

    def list_dir(self, path, tok, page_size=100):
        return []


@pytest.fixture
def args():
    return Namespace(url="https://pan.baidu.com/s/1abc?pwd=p1", pwd=None,
                     dest="/dest", fsids=None, cookies="unused", json=True)


def run_save(monkeypatch, fake, args, capsys):
    monkeypatch.setattr(bdpan_share, "_session", lambda a: fake)
    rc = cmd_save(args)
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_share_files_maps_metadata():
    files = _share_files(SHARE_DATA)
    assert files[0]["isdir"] is True
    assert files[1]["duration"] == 55
    assert files[1]["md5_object_key"] == "objkey"


def test_save_folder_recurses_and_files_carry_metadata(monkeypatch, args, capsys):
    fake = FakeSession()
    out = run_save(monkeypatch, fake, args, capsys)
    saved = {s["relpath"]: s for s in out["saved"]}
    # folder share → walked subtree with folder-prefixed relpath
    assert fake.tree_calls == ["/dest/课程"]
    assert saved["课程/x.mov"]["size"] == 50
    assert saved["课程/x.mov"]["duration"] is None
    # plain file → to_fs_id + duration from share metadata
    assert saved["a.mov"]["fs_id"] == 444
    assert saved["a.mov"]["duration"] == 55
    assert saved["a.mov"]["size"] == 100


def test_save_fsids_subset(monkeypatch, args, capsys):
    args.fsids = "333"
    fake = FakeSession(transfer_out={
        "errno": 0,
        "extra": {"list": [
            {"from": "/a.mov", "from_fs_id": 333, "to": "/dest/a.mov", "to_fs_id": 444}]},
    })
    out = run_save(monkeypatch, fake, args, capsys)
    assert [s["relpath"] for s in out["saved"]] == ["a.mov"]
    assert fake.tree_calls == []


def test_save_transfer_error_raises(monkeypatch, args, capsys):
    from bdpan_common import BaiduError
    fake = FakeSession(transfer_out={"errno": -33, "info": []})
    with pytest.raises(BaiduError):
        run_save(monkeypatch, fake, args, capsys)
