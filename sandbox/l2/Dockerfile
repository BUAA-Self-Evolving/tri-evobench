FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*

# Dependencies declared by l2_demo/runtime/pyproject.toml. The benchmark package
# itself is mounted read-only at runtime so private evaluators never enter the image.
RUN pip install --no-cache-dir \
    'matplotlib>=3.8,<4' \
    'numpy>=2,<3' \
    'openai>=1.50,<4' \
    'pandas>=2.2,<4' \
    'scikit-learn>=1.5,<2' \
    'seaborn>=0.13,<1' \
    'tree-sitter>=0.22,<0.27' \
    'tree-sitter-python>=0.21,<0.26'

WORKDIR /work
CMD ["sleep", "infinity"]
