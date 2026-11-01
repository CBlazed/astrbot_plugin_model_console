"""测试专用：真实 AstrBot 不可用时注入最小 ``astrbot.api.logger`` 桩。

生产代码里的日志记录器**只能**从 ``astrbot.api`` 导入（插件上架规范），因此
``console/app.py`` 顶层是无条件导入。为了让没有安装 AstrBot 的测试环境也能导入
插件模块，本桩在真实包缺失时注册一个最小的 ``astrbot`` / ``astrbot.api`` 模块，
其 ``logger`` 只把调用记录到内存（**不使用标准库日志模块**）。

真实 AstrBot 环境（插件运行时）不会走到这里：``install()`` 检测到包已存在就跳过。
"""

from __future__ import annotations

import sys
import types
from typing import Any

#: 桩 logger 记录的一条日志：(level, message, args, kwargs)。
LogRecord = tuple[str, str, tuple[Any, ...], dict[str, Any]]


class RecordingLogger:
    """最小 logger 桩，实现 AstrBot 日志对象上插件会用到的方法。"""

    def __init__(self) -> None:
        self.records: list[LogRecord] = []

    def _write(
        self,
        level: str,
        message: object,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self.records.append((level, str(message), args, kwargs))

    def debug(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("debug", message, args, kwargs)

    def info(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("info", message, args, kwargs)

    def warning(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("warning", message, args, kwargs)

    def error(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("error", message, args, kwargs)

    def exception(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("exception", message, args, kwargs)

    def critical(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("critical", message, args, kwargs)

    def log(self, level: object, message: object, *args: Any, **kwargs: Any) -> None:
        self._write(str(level), message, args, kwargs)

    def clear(self) -> None:
        """清空已记录日志（用例间隔离）。"""
        self.records.clear()


#: 测试可导入的记录器实例。
LOGGER = RecordingLogger()


def install() -> bool:
    """真实 AstrBot 缺失时安装最小桩。

    Returns:
        安装了桩返回 ``True``；真实包已存在（或已安装过桩）返回 ``False``。
    """
    if "astrbot" in sys.modules:
        return False
    try:  # 真实环境：尊重真实包，绝不覆盖。
        import astrbot  # noqa: F401  # pylint: disable=unused-import
    except Exception:  # noqa: BLE001 - 未安装 AstrBot 是单测环境的正常情况
        pass
    else:
        return False

    package = types.ModuleType("astrbot")
    package.__path__ = []  # 声明为包，子模块缺失时按正常 ImportError 处理
    api = types.ModuleType("astrbot.api")
    api.logger = LOGGER
    package.api = api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = package
    sys.modules["astrbot.api"] = api
    return True
