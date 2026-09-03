FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements*.txt .
RUN pip install --no-cache-dir -r requirements-build.txt \
    && pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.txt

COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

RUN groupadd -g 1000 selu && useradd -u 1000 -g 1000 -m selu

WORKDIR /app

COPY server.py healthcheck.py ./
COPY schulessen_client.py .
COPY --from=builder /build/capability_pb2.py .
COPY --from=builder /build/capability_pb2_grpc.py .

USER 1000:1000

EXPOSE 50051

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "server.py"]
