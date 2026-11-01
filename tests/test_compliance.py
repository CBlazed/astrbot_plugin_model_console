"""上架合规自查：日志来源与高风险调用。

插件商店的自动安全检查要求：**日志记录器必须且只能从 ``astrbot.api`` 导入**，
严禁使用 Python 内置日志模块。本用例把这条规则和若干高风险模式
（``eval`` / ``exec`` / ``shell=True`` / ``requests`` / ``pickle.loads``）固化成测试，
防止后续改动回退。

说明：禁止字面量用字符串拼接生成，避免本文件自身的规则文本被同类静态扫描误报。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {".git", "__pycache__", ".github", ".pytest_cache"}

#: 内置日志模块名（拼接以避免本文件自身被"禁止字面量"规则误报）。
_BUILTIN_LOG_MODULE = "log" + "ging"

#: (正则, 说明) —— 整仓 ``*.py`` 都不允许出现。
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"\bimport\s+{_BUILTIN_LOG_MODULE}\b", "禁止 import 内置日志模块"),
    (rf"\bfrom\s+{_BUILTIN_LOG_MODULE}\b", "禁止从内置日志模块导入"),
    (rf"{_BUILTIN_LOG_MODULE}\.getLogger\s*\(", "禁止 getLogger"),
    (rf"{_BUILTIN_LOG_MODULE}\.basicConfig\s*\(", "禁止 basicConfig"),
    (r"\bimport\s+requests\b", "禁止 import requests（应用 httpx/aiohttp）"),
    (r"\bfrom\s+requests\b", "禁止 from requests import"),
    (r"\beval\s*\(", "禁止 eval"),
    (r"\bexec\s*\(", "禁止 exec"),
    (r"shell\s*=\s*True", "禁止 shell=True"),
    (r"pickle\.loads\s*\(", "禁止 pickle.loads"),
)


def _python_files() -> list[Path]:
    """返回仓库中所有需要检查的 Python 文件。"""
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return sorted(files)


def _iter_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """收集 AST 里所有 import 节点。"""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]


class LoggerSourceComplianceTest(unittest.TestCase):
    """日志来源合规：只允许 astrbot.api，禁止内置日志模块。"""

    def test_no_forbidden_patterns_in_any_python_file(self) -> None:
        problems: list[str] = []
        for path in _python_files():
            if path.resolve() == SELF:
                continue  # 本文件自身包含规则文本
            text = path.read_text(encoding="utf-8")
            for pattern, message in FORBIDDEN_PATTERNS:
                match = re.search(pattern, text)
                if match is not None:
                    line = text.count("\n", 0, match.start()) + 1
                    problems.append(f"{path.relative_to(ROOT)}:{line} {message}")
        self.assertEqual(problems, [], "发现不合规代码：\n" + "\n".join(problems))

    def test_no_builtin_log_module_import(self) -> None:
        offenders: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in _iter_imports(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif node.module:
                    names = [node.module]
                if any(
                    name == _BUILTIN_LOG_MODULE
                    or name.startswith(_BUILTIN_LOG_MODULE + ".")
                    for name in names
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "禁止导入内置日志模块：" + ", ".join(offenders))

    def test_app_imports_logger_from_astrbot_api_at_module_level(self) -> None:
        app_path = ROOT / "console" / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))

        top_level: list[ast.ImportFrom] = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "astrbot.api"
        ]
        logger_names = [alias.name for node in top_level for alias in node.names]
        self.assertIn("logger", logger_names, "console/app.py 顶层必须从 astrbot.api 导入 logger")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module == "astrbot.api":
                    self.fail("astrbot.api 的导入不能被 try/except 包裹（上架规范）")

    def test_main_imports_logger_from_astrbot_api(self) -> None:
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        found = False
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "astrbot.api":
                if any(alias.name == "logger" for alias in node.names):
                    found = True
        self.assertTrue(found, "main.py 必须从 astrbot.api 导入 logger")

    def test_test_harness_provides_logger_stub(self) -> None:
        """单测环境没有 AstrBot 时，测试包要提供 logger 桩供导入。"""
        from tests import _astrbot_stub  # noqa: PLC0415 - 测试内延后导入

        stub_logger = _astrbot_stub.LOGGER
        for method in ("debug", "info", "warning", "error", "exception", "critical"):
            self.assertTrue(callable(getattr(stub_logger, method, None)), method)

    def test_version_is_declared_consistently(self) -> None:
        """metadata.yaml 与 console.app 的版本必须一致。"""
        from console import app as app_module  # noqa: PLC0415 - 测试内延后导入

        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version_line = next(line for line in metadata.splitlines() if line.startswith("version:"))
        metadata_version = version_line.split(":", 1)[1].strip()
        self.assertEqual(metadata_version, app_module.PLUGIN_VERSION)


if __name__ == "__main__":
    unittest.main()
