FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/trievo
COPY pyproject.toml /opt/trievo/pyproject.toml
COPY trievo_bench/ /opt/trievo/trievo_bench/
COPY baseline/SkillOpt/ /opt/trievo/baseline/SkillOpt/
COPY baseline/adapters/skillopt/ /opt/trievo/baseline/adapters/skillopt/

RUN pip install --no-cache-dir -r /opt/trievo/baseline/SkillOpt/requirements.txt \
    && pip install --no-cache-dir \
        'matplotlib>=3.8,<4' 'pandas>=2.2,<4' 'scikit-learn>=1.5,<2' \
        'scipy>=1.13,<2' 'seaborn>=0.13,<1' \
    && pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/opt/trievo/baseline/SkillOpt:/opt/trievo
ENTRYPOINT ["python", "/opt/trievo/baseline/adapters/skillopt/bridge.py"]
