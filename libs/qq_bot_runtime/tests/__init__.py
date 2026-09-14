# -*- coding: utf-8 -*-
r"""qq_bot 回归测试包（f:\qq_bot\tests）。

运行方式（在 f:\qq_bot 下）：
    venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
或：
    venv\Scripts\python.exe tests\run.py

这些用例固化了前几步的冒烟验证（配置中心 / 路由 / 遥测），
全部为纯逻辑测试，不发起真实网络请求、不读取真实 API 密钥。
"""
