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

# A real L2 agent works in this image, while the private verifier runs in the
# separate l2 runtime image.  Keep the public task runtime available here too:
# otherwise an agent spends its bounded tool budget discovering that packages
# promised by the task (for example scikit-learn) are absent.
RUN pip install --no-cache-dir \
    'matplotlib>=3.8,<4' \
    'numpy>=2,<3' \
    'openai>=1.50,<4' \
    'pandas>=2.2,<4' \
    'scikit-learn>=1.5,<2' \
    'seaborn>=0.13,<1' \
    'tree-sitter>=0.22,<0.27' \
    'tree-sitter-python>=0.21,<0.26'

WORKDIR /opt/trievo
COPY pyproject.toml /opt/trievo/pyproject.toml
COPY trievo_bench/ /opt/trievo/trievo_bench/
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/opt/hermes:/opt/trievo
ENTRYPOINT ["python", "-m", "trievo_bench.baselines.hermes_bridge"]
