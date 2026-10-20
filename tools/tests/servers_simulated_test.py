"""Smoke test: every tool server under tools/*/server.py imports, configures in
simulated mode and answers GetStatus over a real gRPC channel.

Servers whose driver cannot be imported on this machine (Windows-only COM /
pythoncom modules, or vendor .NET SDKs such as SoftMax Pro that are not
installed on CI runners) are skipped and listed; the rest must pass, and at
least MIN_PASSING servers have to be exercised so the test is never vacuous.
"""

import importlib
import inspect
import os
import socket
import sys
import typing as t
import unittest
from concurrent import futures

import grpc
from grpc_reflection.v1alpha import reflection, reflection_pb2, reflection_pb2_grpc

from tools.base_server import ToolServer
from tools.grpc_interfaces import tool_base_pb2, tool_driver_pb2, tool_driver_pb2_grpc

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_PASSING = 15

TOOL_NAMES = sorted(
    name
    for name in os.listdir(TOOLS_DIR)
    if os.path.isfile(os.path.join(TOOLS_DIR, name, "server.py"))
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_class(tool_name: str) -> type[ToolServer]:
    module = importlib.import_module(f"tools.{tool_name}.server")
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, ToolServer) and obj is not ToolServer and obj.__module__ == module.__name__:
            return t.cast(type[ToolServer], obj)
    raise AssertionError(f"tools.{tool_name}.server defines no ToolServer subclass")


class TestServersSimulated(unittest.TestCase):
    def test_tool_servers_discovered(self) -> None:
        self.assertIn("plr", TOOL_NAMES)
        self.assertIn("toolbox", TOOL_NAMES)

    def test_every_server_starts_simulated(self) -> None:
        skipped: list[str] = []
        passed: list[str] = []
        for tool_name in TOOL_NAMES:
            with self.subTest(tool=tool_name):
                try:
                    cls = _server_class(tool_name)
                except Exception as exc:  # ImportError, NotImplementedError, .NET FileNotFoundException
                    skipped.append(f"{tool_name}: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
                    continue
                self._check_server(tool_name, cls)
                passed.append(tool_name)
        print(f"\n{sys.platform}: {len(passed)} servers started simulated, {len(skipped)} skipped")
        for line in skipped:
            print(f"  skipped {line}")
        self.assertGreaterEqual(len(passed), MIN_PASSING, skipped)

    def _check_server(self, tool_name: str, cls: type[ToolServer]) -> None:
        tool_server = cls()
        config = tool_base_pb2.Config(simulated=True, toolId=f"{tool_name}-sim")
        getattr(config, tool_server.toolType).SetInParent()

        reply = tool_server.Configure(config, None)  # type: ignore[arg-type]
        self.assertEqual(reply.response, tool_base_pb2.SUCCESS, tool_name)
        status = tool_server.GetStatus(config, None)  # type: ignore[arg-type]
        self.assertEqual(status.status, tool_base_pb2.SIMULATED, tool_name)

        port = _free_port()
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        tool_driver_pb2_grpc.add_ToolDriverServicer_to_server(tool_server, server)
        service_name = tool_driver_pb2.DESCRIPTOR.services_by_name["ToolDriver"].full_name
        reflection.enable_server_reflection([service_name, reflection.SERVICE_NAME], server)
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = tool_driver_pb2_grpc.ToolDriverStub(channel)
                remote_status = stub.GetStatus(config, timeout=10)
                self.assertEqual(remote_status.status, tool_base_pb2.SIMULATED, tool_name)

                reflection_stub = reflection_pb2_grpc.ServerReflectionStub(channel)
                request = reflection_pb2.ServerReflectionRequest(list_services="")
                responses = list(reflection_stub.ServerReflectionInfo(iter([request]), timeout=10))
                services = {s.name for s in responses[0].list_services_response.service}
                self.assertIn(service_name, services, tool_name)
        finally:
            server.stop(0)


if __name__ == "__main__":
    unittest.main()
