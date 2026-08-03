# syntax=docker/dockerfile:1.7
FROM rust:1.88-bookworm

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
      libappindicator3-dev \
      librsvg2-dev \
      libwebkit2gtk-4.1-dev \
      patchelf \
    && rm -rf /var/lib/apt/lists/*

RUN rustup component add rustfmt

WORKDIR /app

COPY apps/desktop/src-tauri/Cargo.toml apps/desktop/src-tauri/Cargo.lock ./
COPY apps/desktop/src-tauri/build.rs ./build.rs
COPY apps/desktop/src-tauri/src ./src
COPY apps/desktop/src-tauri/capabilities ./capabilities
COPY apps/desktop/src-tauri/icons ./icons
COPY apps/desktop/src-tauri/tauri.conf.json ./tauri.conf.json

RUN cargo fmt --check \
    && cargo check --locked
