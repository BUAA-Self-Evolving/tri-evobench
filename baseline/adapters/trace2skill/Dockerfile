FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/trievo
COPY pyproject.toml /opt/trievo/pyproject.toml
COPY trievo_bench/ /opt/trievo/trievo_bench/
COPY baseline/Trace2Skill/ /opt/trievo/baseline/Trace2Skill/
COPY baseline/adapters/trace2skill/ /opt/trievo/baseline/adapters/trace2skill/

RUN pip install --no-cache-dir openai tqdm openpyxl requests diskcache \
        'matplotlib>=3.8,<4' 'numpy>=2,<3' 'pandas>=2.2,<4' \
        'scikit-learn>=1.5,<2' 'scipy>=1.13,<2' 'seaborn>=0.13,<1' \
    && pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/opt/trievo/baseline/Trace2Skill:/opt/trievo/baseline/Trace2Skill/src:/opt/trievo
ENTRYPOINT ["python", "/opt/trievo/baseline/adapters/trace2skill/bridge.py"]
