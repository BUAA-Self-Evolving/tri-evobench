FROM node:24.14.0-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ git bash ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10.33.2
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 HUSKY=0
WORKDIR /opt/zcode
COPY baseline/ZCode/ ./
RUN pnpm install --frozen-lockfile \
    && pnpm --dir apps/zcode-cli exec turbo run build --filter=@zcode/cli
WORKDIR /opt/trievo
COPY baseline/adapters/native_runtime.py baseline/adapters/native_runtime.py
COPY baseline/adapters/zcode/ baseline/adapters/zcode/
ENTRYPOINT ["python3", "-m", "baseline.adapters.zcode.bridge"]
