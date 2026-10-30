FROM node:24.14.0-bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv make g++ git bash ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@11.28.5
WORKDIR /opt/deepseek-harness
COPY baseline/deepseek-harness/ ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile
# Compile and bundle the Host/CLI face; the Electron desktop bundle is not
# needed by the native stdio SDK and has separate desktop asset prerequisites.
RUN pnpm run build:native-system \
    && pnpm exec tsx scripts/compile-referenced-projects.ts host \
    && pnpm exec tsdown --config-loader native --env.DSH_BUILD_FACE host
RUN python3 -m venv /opt/python \
    && /opt/python/bin/pip install 'pydantic>=2.12,<3' \
    && /opt/python/bin/pip install --no-deps ./python/sdk
ENV PATH="/opt/python/bin:${PATH}"
WORKDIR /opt/trievo
COPY baseline/adapters/native_runtime.py baseline/adapters/native_runtime.py
COPY baseline/adapters/deepseek_harness/ baseline/adapters/deepseek_harness/
ENTRYPOINT ["python", "-m", "baseline.adapters.deepseek_harness.bridge"]
