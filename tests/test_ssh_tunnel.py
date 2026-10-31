"""``console.ssh_tunnel`` 单元测试：argv 形态、端口发现、失败码、清理。

不启动真实 ssh、不连网络：spawn / probe / discovery runner 全部注入。
唯一真实系统调用是 ``find_free_port``（绑 loopback 后立即释放）。
"""

from __future__ import annotations

import asyncio
import unittest

from console import ssh_tunnel
from console.errors import ConsoleError
from console.ssh_tunnel import (
    PORT_UNKNOWN,
    SSH_FAILED,
    SSH_UNAVAILABLE,
    SshTunnel,
    find_free_port,
    resolve_remote_port,
)

SSH = {"host": "1.2.3.4", "port": 2222, "user": "Administrator",
       "keyPath": "C:\\keys\\id_ed25519"}


class FakeProcess:
    """最小子进程替身：记录信号并让 ``wait()`` 可被唤醒。"""

    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.kills: list[str] = []
        self._done = asyncio.Event()
        if returncode is not None:
            self._done.set()

    def terminate(self) -> None:
        self.kills.append("SIGTERM")
        self.returncode = 0
        self._done.set()

    def kill(self) -> None:
        self.kills.append("SIGKILL")
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0


async def always_ready(_port: int) -> bool:
    return True


async def never_ready(_port: int) -> bool:
    return False


def spawner_recording(spawned: list, process: FakeProcess):
    """返回一个记录 argv 的 spawner。"""

    async def spawn(argv: list[str]):
        spawned.append(argv)
        return process

    return spawn


class PortDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """远端端口从日志最后一条 ``Starting WebUI at`` 解析。"""

    async def test_last_matching_line_wins_and_argv_is_expected(self) -> None:
        calls: list[list[str]] = []

        async def run(argv: list[str]) -> str:
            calls.append(argv)
            return "\r\n".join([
                "[2026-10-29 09:00:00] INFO: Starting WebUI at http://127.0.0.1:6100",
                "[2026-10-29 12:00:00] INFO: Starting WebUI at http://localhost:7001",
            ])

        port = await resolve_remote_port(SSH, data_path="C:\\AstrBot\\data", run=run)
        self.assertEqual(port, 7001, "last matching line must win")
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertTrue(all(isinstance(value, str) for value in argv))
        self.assertEqual(argv[0:2], ["-p", "2222"])
        self.assertEqual(argv[2:4], ["-i", "C:\\keys\\id_ed25519"])
        self.assertEqual(argv[4:6], ["-o", "BatchMode=yes"])
        self.assertEqual(argv[7], "ConnectTimeout=10")
        self.assertEqual(argv[8], "Administrator@1.2.3.4")
        command = argv[-1]
        self.assertTrue(
            command.startswith('powershell -NoProfile -NonInteractive -Command "'))
        self.assertIn("-LiteralPath 'C:\\AstrBot\\data\\logs\\astrbot.log'", command)
        self.assertIn("'Starting WebUI at'", command)
        self.assertIn("Select-Object -Last 1", command)

    async def test_spaces_and_apostrophes_are_powershell_literals(self) -> None:
        captured: list[list[str]] = []

        async def run(argv: list[str]) -> str:
            captured.append(argv)
            return "Starting WebUI at http://127.0.0.1:6185\n"

        await resolve_remote_port({"host": "1.2.3.4", "user": "Administrator"},
                                  data_path="C:\\Astr Bot\\It's data", run=run)
        command = captured[0][-1]
        self.assertIn("'C:\\Astr Bot\\It''s data\\logs\\astrbot.log'", command)
        self.assertEqual(captured[0][0:2], ["-p", "22"])
        self.assertNotIn("-i", captured[0])
        self.assertEqual(captured[0][-2], "Administrator@1.2.3.4")

    async def test_accepts_stdout_dict_runner_result(self) -> None:
        async def run(_argv: list[str]) -> dict:
            return {"stdout": "INFO: Starting WebUI at http://127.0.0.1:6185\n"}

        port = await resolve_remote_port({"host": "h", "user": "u"},
                                         data_path="C:\\data", run=run)
        self.assertEqual(port, 6185)

    async def test_failures_always_map_to_port_unknown(self) -> None:
        cases = [
            ("ssh exec rejects", {"ssh": SSH, "data_path": "C:\\data",
                                  "run": None}),
            ("no matching line", {"ssh": SSH, "data_path": "C:\\data",
                                  "run": None}),
            ("empty output", {"ssh": SSH, "data_path": "C:\\data", "run": None}),
            ("missing dataPath", {"ssh": SSH, "run": None}),
            ("unusable ssh host", {"ssh": {"user": "u"}, "data_path": "C:\\data",
                                   "run": None}),
            ("port out of range", {"ssh": SSH, "data_path": "C:\\data",
                                   "run": None}),
        ]

        async def ssh_error(_argv: list[str]) -> str:
            raise RuntimeError("ssh: connect refused SECRET")

        async def nothing(_argv: list[str]) -> str:
            return "INFO: nothing about a dashboard here\n"

        async def empty(_argv: list[str]) -> str:
            return ""

        async def good(_argv: list[str]) -> str:
            return "Starting WebUI at http://127.0.0.1:6185\n"

        async def out_of_range(_argv: list[str]) -> str:
            return "Starting WebUI at http://127.0.0.1:99999\n"

        runners = [ssh_error, nothing, empty, good, good, out_of_range]
        for (label, options), runner in zip(cases, runners):
            with self.subTest(label=label):
                with self.assertRaises(ConsoleError) as ctx:
                    await resolve_remote_port(options["ssh"],
                                              data_path=options.get("data_path"),
                                              run=runner)
                self.assertEqual(ctx.exception.code, PORT_UNKNOWN)


class SshTunnelTests(unittest.IsolatedAsyncioTestCase):
    """隧道建立、就绪探测、清理与错误映射。"""

    async def test_find_free_port(self) -> None:
        port = await find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)
        self.assertLess(port, 65536)

    async def test_argv_contains_forwarding_flags_and_key(self) -> None:
        spawned: list[list[str]] = []
        process = FakeProcess()
        tunnel = SshTunnel(SSH, remote_port=6185, local_port=40001,
                           spawn=spawner_recording(spawned, process),
                           probe=always_ready)
        url = await tunnel.start()
        self.assertEqual(url, "http://127.0.0.1:40001")
        self.assertEqual(spawned, [[
            "-N",
            "-L", "127.0.0.1:40001:127.0.0.1:6185",
            "-p", "2222",
            "-i", "C:\\keys\\id_ed25519",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "Administrator@1.2.3.4",
        ]], "ssh must be an argv array, never a shell string")
        await tunnel.close()

    async def test_defaults_port_22_and_custom_remote_host(self) -> None:
        spawned: list[list[str]] = []
        process = FakeProcess()
        tunnel = SshTunnel({"host": "cloud.example", "user": "ops"},
                           remote_port=7001, local_port=41000,
                           remote_host="10.0.0.5",
                           spawn=spawner_recording(spawned, process),
                           probe=always_ready)
        await tunnel.start()
        argv = spawned[0]
        self.assertEqual(argv[0:5],
                         ["-N", "-L", "127.0.0.1:41000:10.0.0.5:7001", "-p", "22"])
        self.assertNotIn("-i", argv)
        self.assertEqual(argv[-1], "ops@cloud.example")
        await tunnel.close()

    async def test_allocates_free_local_port_when_missing(self) -> None:
        spawned: list[list[str]] = []
        process = FakeProcess()
        tunnel = SshTunnel(SSH, remote_port=6185,
                           spawn=spawner_recording(spawned, process),
                           probe=always_ready)
        url = await tunnel.start()
        local_port = int(url.rsplit(":", 1)[1])
        self.assertGreater(local_port, 0)
        self.assertEqual(spawned[0][2],
                         f"127.0.0.1:{local_port}:127.0.0.1:6185")
        await tunnel.close()

    async def test_waits_through_failed_probes(self) -> None:
        spawned: list[list[str]] = []
        process = FakeProcess()
        probes = 0

        async def probe(_port: int) -> bool:
            nonlocal probes
            probes += 1
            return probes >= 3

        tunnel = SshTunnel(SSH, remote_port=6185, local_port=42000,
                           spawn=spawner_recording(spawned, process),
                           probe=probe, retry_delay_ms=2)
        await tunnel.start()
        self.assertEqual(probes, 3)
        self.assertEqual(tunnel.local_port, 42000)
        await tunnel.close()

    async def test_timeout_maps_to_ssh_failed_and_kills_process(self) -> None:
        process = FakeProcess()
        tunnel = SshTunnel(SSH, remote_port=6185, local_port=42001,
                           spawn=spawner_recording([], process),
                           probe=never_ready, ready_timeout_ms=40,
                           retry_delay_ms=5)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(ConsoleError) as ctx:
            await tunnel.start()
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(ctx.exception.code, SSH_FAILED)
        self.assertLess(elapsed, 2.0, "timeout must not wait the default 10s")
        self.assertEqual(process.kills, ["SIGTERM"])

    async def test_missing_ssh_binary_maps_to_unavailable(self) -> None:
        async def spawn(_argv: list[str]):
            raise FileNotFoundError("spawn ssh ENOENT")

        tunnel = SshTunnel(SSH, remote_port=6185, local_port=42002,
                           spawn=spawn, probe=always_ready)
        with self.assertRaises(ConsoleError) as ctx:
            await tunnel.start()
        self.assertEqual(ctx.exception.code, SSH_UNAVAILABLE)

    async def test_other_spawn_failure_maps_to_ssh_failed(self) -> None:
        async def spawn(_argv: list[str]):
            raise PermissionError("EACCES")

        tunnel = SshTunnel(SSH, remote_port=6185, local_port=42003,
                           spawn=spawn, probe=always_ready)
        with self.assertRaises(ConsoleError) as ctx:
            await tunnel.start()
        self.assertEqual(ctx.exception.code, SSH_FAILED)

    async def test_early_exit_maps_to_ssh_failed(self) -> None:
        process = FakeProcess(returncode=255)
        tunnel = SshTunnel(SSH, remote_port=6185, local_port=42004,
                           spawn=spawner_recording([], process),
                           probe=never_ready, ready_timeout_ms=500,
                           retry_delay_ms=5)
        with self.assertRaises(ConsoleError) as ctx:
            await tunnel.start()
        self.assertEqual(ctx.exception.code, SSH_FAILED)

    async def test_invalid_input_fails_before_spawning(self) -> None:
        spawned: list[list[str]] = []

        async def spawn(argv: list[str]):
            spawned.append(argv)
            return FakeProcess()

        for kwargs in (
            {"ssh": {"host": "h"}, "remote_port": 6185, "local_port": 42006},
            {"ssh": SSH, "remote_port": 0, "local_port": 42007},
            {"ssh": SSH, "remote_port": 70000, "local_port": 42008},
        ):
            with self.subTest(**{k: v for k, v in kwargs.items() if k != "ssh"}):
                tunnel = SshTunnel(kwargs.pop("ssh"), spawn=spawn,
                                   probe=always_ready, **kwargs)
                with self.assertRaises(ConsoleError) as ctx:
                    await tunnel.start()
                self.assertEqual(ctx.exception.code, SSH_FAILED)
        self.assertEqual(spawned, [], "invalid input must fail before spawning ssh")

    async def test_close_is_idempotent_and_only_terminates_once(self) -> None:
        process = FakeProcess()
        tunnel = SshTunnel(SSH, remote_port=6185, local_port=42009,
                           spawn=spawner_recording([], process),
                           probe=always_ready)
        await tunnel.start()
        await tunnel.close()
        await tunnel.close()
        await tunnel.close()
        self.assertEqual(process.kills, ["SIGTERM"])

    async def test_context_manager_returns_url_and_cleans_up(self) -> None:
        process = FakeProcess()
        async with SshTunnel(SSH, base_url="http://127.0.0.1:6185",
                             spawn=spawner_recording([], process),
                             probe=always_ready) as url:
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            local_port = int(url.rsplit(":", 1)[1])
            self.assertEqual(process.kills, [])
            del local_port
        self.assertEqual(process.kills, ["SIGTERM"])

    async def test_base_url_port_used_without_data_path(self) -> None:
        spawned: list[list[str]] = []
        process = FakeProcess()
        tunnel = SshTunnel(SSH, base_url="http://127.0.0.1:6185",
                           spawn=spawner_recording(spawned, process),
                           probe=always_ready)
        url = await tunnel.start()
        local_port = int(url.rsplit(":", 1)[1])
        self.assertEqual(spawned[0][2], f"127.0.0.1:{local_port}:127.0.0.1:6185")
        await tunnel.close()

    async def test_data_path_rediscovers_and_overrides_base_url(self) -> None:
        spawned: list[list[str]] = []
        process = FakeProcess()
        calls: list[list[str]] = []

        async def run(argv: list[str]) -> str:
            calls.append(argv)
            return "INFO: Starting WebUI at http://127.0.0.1:7001\n"

        tunnel = SshTunnel(SSH, base_url="http://127.0.0.1:6185",
                           data_path="C:\\AstrBot\\data", run=run,
                           spawn=spawner_recording(spawned, process),
                           probe=always_ready)
        url = await tunnel.start()
        local_port = int(url.rsplit(":", 1)[1])
        self.assertEqual(len(calls), 1)
        self.assertEqual(spawned[0][2],
                         f"127.0.0.1:{local_port}:127.0.0.1:7001",
                         "discovered port must override the placeholder")
        await tunnel.close()

    async def test_discovery_failure_surfaces_and_never_spawns(self) -> None:
        spawned: list[list[str]] = []

        async def run(_argv: list[str]) -> str:
            return "no dashboard here\n"

        tunnel = SshTunnel(SSH, base_url="http://127.0.0.1:6185",
                           data_path="C:\\data", run=run,
                           spawn=spawner_recording(spawned, FakeProcess()),
                           probe=always_ready)
        with self.assertRaises(ConsoleError) as ctx:
            await tunnel.start()
        self.assertEqual(ctx.exception.code, PORT_UNKNOWN)
        self.assertEqual(spawned, [])

    async def test_unusable_base_url_rejected_before_work(self) -> None:
        spawned: list[list[str]] = []
        for base_url in ("http://127.0.0.1", "not a url"):
            with self.subTest(base_url=base_url):
                tunnel = SshTunnel(SSH, base_url=base_url,
                                   spawn=spawner_recording(spawned, FakeProcess()),
                                   probe=always_ready)
                with self.assertRaises(ConsoleError) as ctx:
                    await tunnel.start()
                self.assertEqual(ctx.exception.code, SSH_FAILED)
        self.assertEqual(spawned, [])

    async def test_module_exposes_contract_class(self) -> None:
        self.assertTrue(callable(ssh_tunnel.SshTunnel))
        self.assertTrue(callable(ssh_tunnel.resolve_remote_port))


if __name__ == "__main__":
    unittest.main()
