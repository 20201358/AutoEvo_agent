"""python -m v3.cli 的入口文件。

实际逻辑在 ``v3.cli.__init__`` 里；本文件只做转发，遵循 PEP 338。
"""

from __future__ import annotations

import sys

from .__init__ import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))