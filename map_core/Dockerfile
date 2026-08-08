FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.13.5

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    ENV=prod

WORKDIR /app


RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap && rm -rf /var/lib/apt/lists/*

RUN pip install uv -i https://mirrors.aliyun.com/pypi/simple

COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --compile-bytecode --frozen

COPY . .

RUN mkdir -p logs /data

VOLUME ["/app/logs", "/data"]

EXPOSE 10000

CMD ["uv", "run", "python", "-m", "map_core.main", "--host", "0.0.0.0", "--port", "10000"]
