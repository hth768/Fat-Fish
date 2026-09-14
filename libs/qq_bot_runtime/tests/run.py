# -*- coding: utf-8 -*-
r"""一键运行 qq_bot 回归测试套件。

用法：
    f:\qq_bot\venv\Scripts\python.exe tests\run.py
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(_HERE, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
