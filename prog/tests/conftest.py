# -*- coding: utf-8 -*-
"""pytest 配置：将仓库根目录加入 sys.path，保证 `import prog` 生效。"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)