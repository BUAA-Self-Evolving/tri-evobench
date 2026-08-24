ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hermes
COPY baseline/hermes-agent/ /opt/hermes/
# Hermes intentionally blocks wheel builds from a source checkout; use the
# supported editable-install path inside this runtime image.
RUN pip install --no-cache-dir -e '.[acp]'

WORKDIR /opt/trievo
COPY pyproject.toml /opt/trievo/pyproject.toml
COPY trievo_bench/ /opt/trievo/trievo_bench/
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/opt/hermes:/opt/trievo
ENTRYPOINT ["python", "-m", "trievo_bench.baselines.hermes_bridge"]
