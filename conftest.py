"""pytest 根配置：确保仓库根在 sys.path，使 `import app...` 可用。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
