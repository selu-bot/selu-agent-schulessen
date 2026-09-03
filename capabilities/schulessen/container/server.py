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
MAX_JSON_BYTES = 64 * 1024
TOOL_FIELDS = {
    "get_menu": {"from_date": str, "to_date": str, "include_inactive": bool},
    "get_cart": {"from_date": str, "to_date": str},
    "place_order": {
        "date": str,
        "meal_id": int,
        "quantity": int,
        "outlet_slot_id": int,
        "allow_checkout_existing_cart": bool,
        "components": list,
    },
    "cancel_order": {"date": str, "meal_id": int, "transaction_id": str},
}


def _validate_args(tool_name: str, args: dict) -> None:
    fields = TOOL_FIELDS.get(tool_name)
    if fields is None:
        raise ValueError("Unknown school lunch tool")
    if args.keys() - fields.keys():
        raise ValueError("Tool arguments contain unsupported fields")
    if (
        tool_name in {"place_order", "cancel_order"}
        and not {"date", "meal_id"} <= args.keys()
    ):
        raise ValueError("date and meal_id are required")
    for name, value in args.items():
        if type(value) is not fields[name]:
            raise ValueError(f"Invalid type for {name}")


class CapabilityState:
    def __init__(self) -> None:
        self._client = SchulessenClient()
        self._lock = threading.RLock()

    def invoke(
        self, tool_name: str, args: dict, config: dict, is_active=lambda: True
    ) -> dict:
        _validate_args(tool_name, args)
        username = config.get("USERNAME")
        password = config.get("PASSWORD")
        if (
            not isinstance(username, str)
            or not username.strip()
            or not isinstance(password, str)
            or not password
        ):
            raise SchulessenError("Missing required credentials: USERNAME and PASSWORD")

        if not self._lock.acquire(timeout=5):
            raise SchulessenError("School lunch service is busy. Try again shortly.")
        try:
            if not is_active():
                raise SchulessenError(
                    "Request expired before execution; no action was started."
                )
            self._client.set_credentials(username.strip(), password)

            if tool_name == "get_menu":
                return self._client.get_menu(
                    from_date=args.get("from_date"),
                    to_date=args.get("to_date"),
                    include_inactive=args.get("include_inactive", False),
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
                    meal_id=args["meal_id"],
                    quantity=args.get("quantity", 1),
                    outlet_slot_id=args.get("outlet_slot_id", 1),
                    allow_checkout_existing_cart=args.get(
                        "allow_checkout_existing_cart", False
                    ),
                    components=args.get("components") or [],
                )

            if tool_name == "cancel_order":
                return self._client.cancel_order(
                    meal_date=args["date"],
                    meal_id=args["meal_id"],
                    transaction_id=args.get("transaction_id"),
                )

        finally:
            self._lock.release()

    @staticmethod
    def _summarize_cart(cart: dict) -> str:
        active_count = int(cart.get("active_item_count", 0))
        cancelled_count = int(cart.get("cancelled_item_count", 0))
        pending_count = int(cart.get("pending_item_count", 0))
        unknown_count = int(cart.get("unknown_item_count", 0))
        if pending_count or unknown_count:
            return (
                f"{active_count} confirmed active orders; {pending_count} pending changes; "
                f"{unknown_count} entries with unknown status. Check these before changing orders."
            )

        if active_count and cancelled_count:
            return (
                f"{active_count} active order"
                f"{'' if active_count == 1 else 's'} and {cancelled_count} cancelled "
                f"entr{'y' if cancelled_count == 1 else 'ies'} in this period."
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
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Tool request exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Tool request must contain valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise SchulessenError("Tool arguments must be a JSON object")
    return value


class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):
    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True, message="schulessen ready")

    def Invoke(self, request, context):
        tool = request.tool_name
        log.info("Invoke tool=%s", tool if tool in TOOL_FIELDS else "unknown")
        try:
            args = _decode_json_bytes(request.args_json, {})
            config = _decode_json_bytes(request.config_json, {})
            result = STATE.invoke(tool, args, config, is_active=context.is_active)
            return capability_pb2.InvokeResponse(
                result_json=json.dumps(result).encode("utf-8")
            )
        except (SchulessenError, ValueError) as exc:
            log.warning("Tool invocation failed: %s", type(exc).__name__)
            return capability_pb2.InvokeResponse(error=str(exc))
        except Exception as exc:  # noqa: BLE001
            # No raw payloads, credentials, chained HTTP exceptions or tracebacks.
            log.error("Unexpected tool failure: %s", type(exc).__name__)
            return capability_pb2.InvokeResponse(
                error="Internal school lunch service error. Check service health."
            )

    def StreamInvoke(self, request, context):
        response = self.Invoke(request, context)
        if response.error:
            yield capability_pb2.InvokeChunk(error=response.error, done=True)
        else:
            yield capability_pb2.InvokeChunk(data=response.result_json, done=True)


def serve() -> None:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        maximum_concurrent_rpcs=4,
        options=[
            ("grpc.max_receive_message_length", 2 * MAX_JSON_BYTES + 4096),
            ("grpc.max_send_message_length", 4 * 1024 * 1024),
        ],
    )
    capability_pb2_grpc.add_CapabilityServicer_to_server(CapabilityServicer(), server)
    if not server.add_insecure_port(f"0.0.0.0:{GRPC_PORT}"):
        raise RuntimeError("Could not bind the capability gRPC port")
    server.start()
    log.info("Schulessen capability listening on port %d", GRPC_PORT)

    def _shutdown(signum, frame):  # noqa: ARG001
        log.info("Shutting down...")
        server.stop(grace=25).wait()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
