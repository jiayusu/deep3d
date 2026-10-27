FROM rust:1.88.0-slim-bookworm@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89 AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY migrations ./migrations
RUN cargo build --release --locked -p runtime-service

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY --from=builder /src/target/release/deep3d-runtime /usr/local/bin/deep3d-runtime
RUN mkdir -p /store/objects /store/runs && chown -R 10001:10001 /store
USER 10001:10001
EXPOSE 8088
ENTRYPOINT ["deep3d-runtime"]
CMD ["serve", "--bind", "0.0.0.0:8088", "--root", "/store"]
