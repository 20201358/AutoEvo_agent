"""``python -m v3`` 的入口文件。

转发到 :mod:`v3.cli`，遵循 PEP 338。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))