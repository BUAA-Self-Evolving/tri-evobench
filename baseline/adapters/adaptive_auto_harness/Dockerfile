ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG APT_ORIGIN=http://deb.debian.org

ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN sed -i "s|http://deb.debian.org|${APT_ORIGIN}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/trievo
COPY pyproject.toml /opt/trievo/pyproject.toml
COPY trievo_bench/ /opt/trievo/trievo_bench/
COPY baseline/adaptive-auto-harness/ /opt/trievo/baseline/adaptive-auto-harness/
COPY baseline/adapters/adaptive_auto_harness/ /opt/trievo/baseline/adapters/adaptive_auto_harness/

RUN pip install --no-cache-dir \
        '/opt/trievo/baseline/adaptive-auto-harness[openai]' \
        'matplotlib>=3.8,<4' 'numpy>=2,<3' 'pandas>=2.2,<4' \
        'scikit-learn>=1.5,<2' 'scipy>=1.13,<2' 'seaborn>=0.13,<1' \
    && pip install --no-cache-dir --no-deps /opt/trievo

ENV PYTHONPATH=/opt/trievo/baseline/adaptive-auto-harness:/opt/trievo
ENTRYPOINT ["python", "/opt/trievo/baseline/adapters/adaptive_auto_harness/bridge.py"]
