# vendored/baidu_pan —— 来源与改动说明

这里的代码**不是本项目的原创**，是从上游项目原样拷贝的，用于百度网盘的
分享解析、转存和断点续传下载。单独记一份来源，方便日后审计和升级。

## 来源

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/skyzhao1223/baidu-pan-skill |
| 分支 / 提交 | `main` @ `e6cc3719dfd6412053fcf68b7f2f3e590ceaacde` |
| 提交时间 | 2026-09-22 |
| 许可 | MIT（见同目录 `LICENSE`） |

## 文件清单

| 文件 | 用途 | 本项目是否使用 |
|---|---|---|
| `bdpan_common.py` | Cookie 过滤、分享页解析、带 Cookie 的 HTTP 会话 | ✅ 直接用 |
| `bdpan_share.py` | 分享 `inspect` / `save`（转存到自己网盘） | ✅ 直接用 |
| `bdpan_download.py` | 经典 PCS 端点分块下载、断点续传、认证刷新 | ✅ 直接用 |
| `bdpan_verify.py` | 下载完整性校验（容器原子结构 + 时长） | ⬜ 未接入（我们只校验大小和时长） |
| `bdpan_cookies.py` | 从浏览器解密 Cookie（**仅 macOS**） | ⬜ Windows 用不到，保留以便跑上游测试 |
| `tests/` | 上游自带的测试套件 | ✅ 见下 |

## 本地改动

1. **布局**：上游把脚本放在 `scripts/` 下，本项目把它们平铺在 `vendor/baidu_pan/`。
   因此 `tests/conftest.py` 的 `sys.path` 多加了一条上一层目录。
   除这一行外，测试文件与上游完全一致（改动见第 4 条之后仍保持这一性质）。
2. **补拷 `bdpan_cookies.py`**：初次 vendoring 时漏掉了（我们不用它），
   后来发现 `tests/test_cookies.py` 需要它才能收集，于是补齐。
3. **`bdpan_common.py` 新增 `Session.search()`**：按文件名在网盘里全局搜索。
   用途见第 4 条。
4. **`bdpan_share.py` 容忍「文件已存在」**：百度转存接口按内容去重，分享里的
   文件早就存在于用户网盘时，接口返回 `errno=2` + `show_msg="文件已存在"`。
   上游把它当失败抛出，于是整个批量中断，而实际上内容已经在了。现在这种情况
   不抛错，改为在目标目录里找不到时**全局搜索**（用第 3 条的方法）把已有文件
   找出来复用。
5. **`bdpan_share.py` 的错误原因取自 `show_msg`**：上游只读 `info`，而百度把人类
   可读的原因放在 `show_msg` / `errmsg` / `err_msg`（`info` 经常是空的）。
   后果是用户看到一句 `BaiduError: [errno 2] []`，完全无法判断发生了什么。
6. **`bdpan_share.py` 建目录前先问存在性**：百度的 `/api/create` 对**已存在**的
   目录不报错，而是新建一个带时间戳的副本（`/x` → `/x_20261005_211106`）。
   反复跑同一批链接会在用户网盘里堆出一串看不出用途的目录。现在用
   `list_dir`（不存在的目录会抛错）先判断，存在就不建。

第 3–6 条都发生在**上游测试覆盖的行为之外**，所以 46 项上游测试仍然原样通过
（`test_save_*` 用的是 FakeSession，`list_dir` 返回空列表即"目录存在"，
因此不会多调一次 `create`）。

## 跑上游测试

```bash
pip install -r requirements-dev.txt
python -m pytest vendor/baidu_pan/tests -q
```

在 Windows + Python 3.12 上的结果：**46 passed, 3 skipped**。
跳过的是 `test_cookies.py` 里依赖 `openssl` CLI 的 3 个用例（macOS Cookie
解密路径），属于环境差异，不是失败。

这套测试覆盖了本项目依赖的关键行为，比自己重写一遍更有说服力：

- `parse_locals_mset`：分享页里那个 `locals.mset({...})` 数据块的解析；
- `save_folder_recurses_and_files_carry_metadata`：**文件夹分享的递归转存**；
- `download_resumes_after_failure` / `download_short_read_retried`：断点续传与短读重试；
- `download_auth_error_without_hook_fails_fast`：**凭证失效时的快速失败**（本项目第 4 轮据此做了批量中止）；
- `list_tree_recurses_with_relpath`：目录树遍历与 `relpath` 生成（本项目用它做嵌套文件的唯一键）；
- `check_content_range`、`verify_truncated_file_fails_atom_tiling`：完整性校验。

## 升级方式

上游更新后，重新拷贝上述文件并重跑 `pytest vendor/baidu_pan/tests`。
注意 `bdpan_download.py` 会把「凭证被拒」包成普通 `OSError`，
本项目在 `bp2md/baidu.py` 里按文案识别它——上游若改了这句文案，
`classify_error()` 的匹配需要同步更新（`tests/selftest.py` 里有用例守着）。
