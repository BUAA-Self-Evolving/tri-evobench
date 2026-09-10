ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG APT_ORIGIN=http://deb.debian.org

ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PRIME_AGENT_KERNEL_PYTHON=/opt/prime-kernel/bin/python

RUN sed -i "s|http://deb.debian.org|${APT_ORIGIN}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git python3 python3-pip python3-venv python-is-python3 ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/trievo
COPY pyproject.toml /opt/trievo/pyproject.toml
COPY trievo_bench/ /opt/trievo/trievo_bench/
COPY baseline/prime-agent/ /opt/trievo/baseline/prime-agent/
COPY baseline/adapters/prime_agent/ /opt/trievo/baseline/adapters/prime_agent/

RUN cd /opt/trievo/baseline/prime-agent \
    && npm ci --ignore-scripts \
    && npm run build \
    && python -m venv /opt/prime-kernel \
    && /opt/prime-kernel/bin/pip install --no-cache-dir \
        /opt/trievo/baseline/prime-agent/prime-agent-runtime \
        dill requests httpx pyyaml tomli python-dotenv pandas numpy scipy beautifulsoup4 lxml pydantic tyro \
    && pip install --no-cache-dir --no-deps /opt/trievo

ENTRYPOINT ["python", "/opt/trievo/baseline/adapters/prime_agent/bridge.py"]
