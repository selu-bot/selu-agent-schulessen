FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN groupadd -g 1000 selu && useradd -u 1000 -g 1000 -m selu

WORKDIR /app

COPY server.py .
COPY schulessen_client.py .
COPY --from=builder /build/capability_pb2.py .
COPY --from=builder /build/capability_pb2_grpc.py .

USER 1000:1000

EXPOSE 50051

CMD ["python", "server.py"]
