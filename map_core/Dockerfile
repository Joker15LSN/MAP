FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.13.5

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    ENV=prod

WORKDIR /app

# P0-SEC-01 (review R-02): bubblewrap removed - host-side sandboxing is not
# part of the target architecture (execution is served by OpenSandbox Server);
# the image runs as an unprivileged fixed UID without any sandbox helper.
RUN pip install uv -i https://mirrors.aliyun.com/pypi/simple

# The project is installed by uv sync, so the build backend needs the
# package source and readme at sync time (no COPY . . anywhere).
COPY pyproject.toml uv.lock README.md ./
COPY map_core ./map_core

RUN uv sync --no-dev --compile-bytecode --frozen

ARG MAP_CORE_VERSION=unknown
ENV MAP_CORE_VERSION=${MAP_CORE_VERSION}

# Non-root runtime: uv must not try to write a cache under / (no home dir
# for UID 10001); everything the runtime needs is already inside /app.
ENV UV_NO_CACHE=1 HOME=/app

RUN mkdir -p logs /data && chown -R 10001:10001 /app

# Non-root runtime user (AC-SEC-06 / review R-02 requirement 5).
USER 10001

VOLUME ["/app/logs", "/data"]

EXPOSE 10000

CMD ["uv", "run", "python", "-m", "map_core.main", "--host", "0.0.0.0", "--port", "10000"]
