# shim/Dockerfile
#
# Two-stage build:
#
#   Stage 1 (proto-builder): pull Envoy + transitive proto sources at pinned
#     tags/commits, run grpc_tools.protoc to generate Python bindings for the
#     ext_proc service. The generated tree lands in /out and is copied into
#     the runtime image at /app/generated.
#
#   Stage 2 (runtime): slim python image, install runtime deps, copy in the
#     shim source and the generated proto bindings.
#
# The proto-generation is deterministic (pinned tags) so a rebuild against
# the same base image produces the same bindings. Vivek can audit every
# proto file that landed in the runtime by copying /app/generated out of
# a running container.

# =========================================================================
# STAGE 1 — proto-builder
# =========================================================================
FROM python:3.12-slim AS proto-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        grpcio==1.66.2 \
        grpcio-tools==1.66.2 \
        protobuf==5.28.2

WORKDIR /src

# Pinned refs for reproducibility. Bump these deliberately when we upgrade.
RUN git clone --depth=1 --branch=v1.32.2 \
        https://github.com/envoyproxy/envoy.git envoy && \
    git clone --depth=1 --branch=v1.1.0 \
        https://github.com/bufbuild/protoc-gen-validate.git pgv && \
    git clone --depth=1 \
        https://github.com/cncf/xds.git xds && \
    git clone --depth=1 \
        https://github.com/googleapis/googleapis.git googleapis && \
    git clone --depth=1 \
        https://github.com/census-instrumentation/opencensus-proto.git opencensus

RUN mkdir -p /out

# Generate Python bindings. Each `cd` puts us inside the include root so
# `find` outputs paths that match how the .proto imports address each other.
#
#     envoy imports use "envoy/..." prefix   → include /src/envoy/api
#     xds imports use "xds/..." prefix       → include /src/xds
#     udpa imports use "udpa/..." prefix     → include /src/xds
#     validate imports use "validate/..."    → include /src/pgv
#     google/api imports                     → include /src/googleapis
#
# `|| true` after each block: some deeply-nested Envoy protos in v1.32
# (watchdog admin bootstrap etc) transitively touch modules we don't need
# for ext_proc. We tolerate their generation failures — server.py only
# imports ext_proc / core / type, and those succeed.

# 1) Envoy API tree — compile one file at a time so a single bad proto
#    doesn't nuke the entire batch. In v1.32 the OpenTelemetry access-logger
#    protos reference otel/common protos we don't ship (we don't need them
#    for ext_proc anyway) — they fail per-file, we skip them, and everything
#    else compiles cleanly.
RUN cd /src/envoy/api && \
    find envoy -type f -name '*.proto' | \
    xargs -n1 -I{} sh -c '\
        python -m grpc_tools.protoc \
            -I/src/envoy/api -I/src/pgv -I/src/xds -I/src/googleapis \
            --python_out=/out --grpc_python_out=/out \
            "{}" 2>/dev/null || echo "SKIP {}" >> /tmp/proto-skips.log \
    ' && \
    echo "=== protos we skipped (transitively OTEL-dependent, unused): ===" && \
    cat /tmp/proto-skips.log 2>/dev/null | head -20 && \
    echo "=== envoy tree generated: ===" && \
    find /out/envoy -type d | wc -l

# 2) xds/udpa proto trees — cncf/xds hosts both under one root. Same per-file
#    compile so one problematic proto doesn't kill the batch.
RUN cd /src/xds && \
    find xds udpa -type f -name '*.proto' 2>/dev/null | \
    xargs -n1 -I{} sh -c '\
        python -m grpc_tools.protoc \
            -I/src/xds -I/src/pgv -I/src/googleapis \
            --python_out=/out \
            "{}" 2>/dev/null || echo "SKIP xds/{}" >> /tmp/proto-skips.log \
    ' && \
    echo "=== xds/udpa generated: ===" && \
    find /out/xds /out/udpa -type d 2>/dev/null | wc -l

# 3) protoc-gen-validate — validate/validate.proto is imported by many envoy protos
RUN cd /src/pgv && \
    python -m grpc_tools.protoc \
        -I/src/pgv \
        --python_out=/out \
        validate/validate.proto

# 4) google/api annotations — used by service definitions
RUN cd /src/googleapis && \
    python -m grpc_tools.protoc \
        -I/src/googleapis \
        --python_out=/out \
        google/api/annotations.proto \
        google/api/http.proto

# 5) opencensus tracing protos (transitively referenced by some envoy protos)
RUN cd /src/opencensus/src && \
    oc_protos=$(find opencensus -type f -name '*.proto' 2>/dev/null) && \
    if [ -n "$oc_protos" ]; then \
        python -m grpc_tools.protoc \
            -I/src/opencensus/src \
            --python_out=/out \
            $oc_protos ; \
        true ; \
    fi

# NOTE — deliberately NOT creating __init__.py under /out. We rely on Python
# 3 namespace packages so `/out/google/api/` merges with the pip-installed
# `google.protobuf` at import time. Adding `__init__.py` to /out/google/
# would shadow the runtime's google.protobuf and break every generated pb2.

# =========================================================================
# STAGE 2 — runtime
# =========================================================================
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Generated proto bindings from stage 1
COPY --from=proto-builder /out /app/generated

COPY server.py .

EXPOSE 50051
CMD ["python", "-u", "server.py"]
