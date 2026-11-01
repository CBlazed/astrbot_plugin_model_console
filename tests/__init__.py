"""插件后端单元测试包（``-m unittest discover -s tests -t .`` 要求 tests 可导入）。

导入测试包时先安装 ``astrbot.api.logger`` 桩：生产代码只允许从 ``astrbot.api``
导入日志记录器（插件上架规范），而 CI/单测环境不安装 AstrBot，需要最小桩才能导入。
真实 AstrBot 环境不会覆盖。
"""

from . import _astrbot_stub

_astrbot_stub.install()
