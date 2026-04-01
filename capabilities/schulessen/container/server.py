import json
import logging
import signal
import sys
import threading
from concurrent import futures

import grpc

import capability_pb2
import capability_pb2_grpc
from schulessen_client import SchulessenClient, SchulessenError


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("schulessen")

GRPC_PORT = 50051


class CapabilityState:
    def __init__(self) -> None:
        self._client = SchulessenClient()
        self._lock = threading.RLock()

    def invoke(self, tool_name: str, args: dict, config: dict) -> dict:
        username = str(config.get("USERNAME") or "").strip()
        password = str(config.get("PASSWORD") or "")
        if not username or not password:
            raise SchulessenError("Missing required credentials: USERNAME and PASSWORD")

        with self._lock:
            self._client.set_credentials(username, password)

            if tool_name == "get_menu":
                return self._client.get_menu(
                    from_date=args.get("from_date"),
                    to_date=args.get("to_date"),
                    include_inactive=bool(args.get("include_inactive", False)),
                )

            if tool_name == "get_cart":
                result = self._client.get_cart_for_range(
                    from_date=args.get("from_date"),
                    to_date=args.get("to_date"),
                )
                result["summary"] = self._summarize_cart(result)
                return result

            if tool_name == "place_order":
                return self._client.place_order(
                    meal_date=args["date"],
                    meal_id=int(args["meal_id"]),
                    quantity=int(args.get("quantity", 1)),
                    outlet_slot_id=int(args.get("outlet_slot_id", 1)),
                    allow_checkout_existing_cart=bool(
                        args.get("allow_checkout_existing_cart", False)
                    ),
                    components=args.get("components") or [],
                )

            if tool_name == "cancel_order":
                return self._client.cancel_order(
                    meal_date=args["date"],
                    meal_id=int(args["meal_id"]),
                    transaction_id=args.get("transaction_id"),
                )

        raise SchulessenError(f"Unknown tool: '{tool_name}'")

    @staticmethod
    def _summarize_cart(cart: dict) -> str:
        active_count = int(cart.get("active_item_count", 0))
        cancelled_count = int(cart.get("cancelled_item_count", 0))

        if active_count and cancelled_count:
            return (
                f"{active_count} active order"
                f"{'' if active_count == 1 else 's'} and {cancelled_count} cancelled "
                f"entr{ 'y' if cancelled_count == 1 else 'ies' } in this period."
            )
        if active_count:
            return f"{active_count} active order{'' if active_count == 1 else 's'} in this period."
        if cancelled_count:
            return (
                f"No active orders. {cancelled_count} cancelled "
                f"entr{'y' if cancelled_count == 1 else 'ies'} in this period."
            )
        return "No orders in this period."


STATE = CapabilityState()


def _decode_json_bytes(raw: bytes, fallback: dict) -> dict:
    if not raw:
        return fallback
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise SchulessenError("Tool arguments must be a JSON object")
    return value


class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):
    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True, message="schulessen ready")

    def Invoke(self, request, context):
        tool = request.tool_name
        log.info("Invoke tool=%s", tool)
        try:
            args = _decode_json_bytes(request.args_json, {})
            config = _decode_json_bytes(request.config_json, {})
            result = STATE.invoke(tool, args, config)
            return capability_pb2.InvokeResponse(
                result_json=json.dumps(result).encode("utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Tool invocation failed")
            return capability_pb2.InvokeResponse(error=str(exc))

    def StreamInvoke(self, request, context):
        response = self.Invoke(request, context)
        if response.error:
            yield capability_pb2.InvokeChunk(error=response.error, done=True)
        else:
            yield capability_pb2.InvokeChunk(data=response.result_json, done=True)


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    capability_pb2_grpc.add_CapabilityServicer_to_server(
        CapabilityServicer(), server
    )
    server.add_insecure_port(f"0.0.0.0:{GRPC_PORT}")
    server.start()
    log.info("Schulessen capability listening on port %d", GRPC_PORT)

    def _shutdown(signum, frame):  # noqa: ARG001
        log.info("Shutting down...")
        server.stop(grace=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
