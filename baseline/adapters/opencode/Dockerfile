ARG BASE_IMAGE=oven/bun:1.3.14-debian
FROM ${BASE_IMAGE}

ARG APT_ORIGIN=http://deb.debian.org

RUN sed -i "s|http://deb.debian.org|${APT_ORIGIN}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git python3 python3-pip python3-venv ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/opencode-python

ENV PATH=/opt/opencode-python/bin:${PATH}

RUN pip install --no-cache-dir \
    'matplotlib>=3.8,<4' \
    'numpy>=2,<3' \
    'openpyxl>=3.1,<4' \
    'pandas>=2.2,<4' \
    'requests>=2.31,<3' \
    'scikit-learn>=1.5,<2' \
    'scipy>=1.13,<2' \
    'seaborn>=0.13,<1'

WORKDIR /opt/opencode
COPY baseline/opencode/ /opt/opencode/
RUN bun install --frozen-lockfile

WORKDIR /opt/trievo
COPY baseline/adapters/opencode/ /opt/trievo/baseline/adapters/opencode/

ENTRYPOINT ["python3", "/opt/trievo/baseline/adapters/opencode/bridge.py"]
