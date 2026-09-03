import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import grpc
    from grpc_tools import protoc
except ImportError:
    raise unittest.SkipTest("Install requirements-build.txt to run gRPC service tests")

CONTAINER = Path(__file__).resolve().parents[1] / "capabilities/schulessen/container"
GENERATED = tempfile.TemporaryDirectory(prefix="schulessen-proto-")
if protoc.main(
    [
        "protoc",
        f"-I{CONTAINER}",
        f"--python_out={GENERATED.name}",
        f"--grpc_python_out={GENERATED.name}",
        str(CONTAINER / "capability.proto"),
    ]
):
    raise RuntimeError("Protobuf generation failed")
sys.path.insert(0, GENERATED.name)
sys.path.insert(0, str(CONTAINER))
import capability_pb2 as pb
import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.state = server.CapabilityState()
        self.state._client = MagicMock()
        self.config = {"USERNAME": "test-user", "PASSWORD": "SECRET"}

    def test_rejects_truthy_string_boolean_without_calling_client(self):
        for tool, args in [
            ("get_menu", {"include_inactive": "false"}),
            (
                "place_order",
                {
                    "date": "2026-09-04",
                    "meal_id": 407,
                    "allow_checkout_existing_cart": "false",
                },
            ),
        ]:
            with self.assertRaises(ValueError):
                self.state.invoke(tool, args, self.config)
        self.state._client.set_credentials.assert_not_called()

    def test_rejects_boolean_as_integer_and_unknown_fields(self):
        for args in [
            {"date": "2026-09-04", "meal_id": True},
            {"date": "2026-09-04", "meal_id": 407, "bypass_checks": True},
            {},
        ]:
            with self.assertRaises(ValueError):
                self.state.invoke("place_order", args, self.config)

    def test_missing_or_non_string_credentials_rejected(self):
        for config in [
            {},
            {"USERNAME": {}, "PASSWORD": "x"},
            {"USERNAME": "x", "PASSWORD": 123},
        ]:
            with self.assertRaises(server.SchulessenError):
                self.state.invoke("get_menu", {}, config)

    def test_credentials_are_bound_before_each_tool_call(self):
        self.state.invoke("get_menu", {}, self.config)
        self.state.invoke(
            "get_menu", {}, {"USERNAME": "other", "PASSWORD": "other-password"}
        )
        self.assertEqual(
            self.state._client.set_credentials.call_args_list[-1].args,
            ("other", "other-password"),
        )

    def test_cancelled_queued_request_does_not_run(self):
        with self.assertRaises(server.SchulessenError):
            self.state.invoke("get_menu", {}, self.config, is_active=lambda: False)
        self.state._client.set_credentials.assert_not_called()

    def test_busy_state_does_not_change_credentials(self):
        self.state._lock = MagicMock()
        self.state._lock.acquire.return_value = False
        with self.assertRaises(server.SchulessenError):
            self.state.invoke("get_menu", {}, self.config)
        self.state._client.set_credentials.assert_not_called()

    def test_json_boundaries(self):
        for raw in [
            b"[]",
            b'"SECRET"',
            b"\xff",
            b"{",
            b"x" * (server.MAX_JSON_BYTES + 1),
        ]:
            with (
                self.subTest(raw_length=len(raw)),
                self.assertRaises((ValueError, server.SchulessenError)),
            ):
                server._decode_json_bytes(raw, {})

    def test_unknown_exception_never_exposes_secret_in_error_or_logs(self):
        request = pb.InvokeRequest(
            tool_name="get_menu", args_json=b"{}", config_json=b"{}"
        )
        with (
            patch.object(server.STATE, "invoke", side_effect=RuntimeError("SECRET")),
            self.assertLogs("schulessen", level="ERROR") as logs,
        ):
            response = server.CapabilityServicer().Invoke(request, MagicMock())
        self.assertNotIn("SECRET", response.error)
        self.assertNotIn("SECRET", str(logs.output))

    def test_pending_summary_cannot_claim_no_orders(self):
        text = server.CapabilityState._summarize_cart(
            {"active_item_count": 0, "pending_item_count": 1}
        )
        self.assertIn("pending", text)
        self.assertNotIn("No orders", text)

    def test_grpc_contract_health_invoke_and_stream(self):
        from concurrent.futures import ThreadPoolExecutor
        import capability_pb2_grpc as rpc

        service = grpc.server(ThreadPoolExecutor(max_workers=2))
        rpc.add_CapabilityServicer_to_server(server.CapabilityServicer(), service)
        port = service.add_insecure_port("127.0.0.1:0")
        service.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = rpc.CapabilityStub(channel)
                self.assertTrue(stub.Healthcheck(pb.HealthRequest(), timeout=3).ready)
                request = pb.InvokeRequest(
                    tool_name="get_menu", args_json=b"{}", config_json=b"{}"
                )
                with patch.object(server.STATE, "invoke", return_value={"days": []}):
                    result = stub.Invoke(request, timeout=3)
                    self.assertEqual(json.loads(result.result_json), {"days": []})
                    chunks = list(stub.StreamInvoke(request, timeout=3))
                    self.assertTrue(chunks[-1].done)
                    self.assertEqual(json.loads(chunks[0].data), {"days": []})
                malformed = stub.Invoke(
                    pb.InvokeRequest(tool_name="get_menu", args_json=b"[]"), timeout=3
                )
                self.assertTrue(malformed.error)
        finally:
            service.stop(0).wait()


if __name__ == "__main__":
    unittest.main()
