import sys
from pathlib import Path

# 上游 layout 是 scripts/ 子目录；本仓库 vendoring 时把脚本放在上一级，
# 所以这里两个路径都加，保持与上游测试兼容。
_here = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_here / "scripts"))
sys.path.insert(0, str(_here))
