FROM rust:1.95-bookworm AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends clang cmake pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/codex
COPY baseline/codex/ /opt/codex/
WORKDIR /opt/codex/codex-rs
RUN cargo build --locked --release -p codex-cli --bin codex

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep libssl3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/codex/codex-rs/target/release/codex /opt/trievo/bin/codex-cli
WORKDIR /opt/trievo
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY baseline/adapters/codex/bridge.py /opt/trievo/adapters/codex_bridge.py

ENTRYPOINT ["python", "/opt/trievo/adapters/codex_bridge.py"]
