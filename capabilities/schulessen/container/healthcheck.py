"""Local liveness check; no credentials or schulessen.net traffic."""

import grpc
import capability_pb2
import capability_pb2_grpc

with grpc.insecure_channel("127.0.0.1:50051") as channel:
    stub = capability_pb2_grpc.CapabilityStub(channel)
    response = stub.Healthcheck(
        capability_pb2.HealthRequest(), timeout=3, wait_for_ready=True
    )
    if not response.ready:
        raise SystemExit(1)
