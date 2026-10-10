# CI-only S3 fixture. Official community images/binaries were withdrawn; build the
# upstream MinIO and mc source releases instead. This image is never deployed.
FROM ghcr.io/pocketcontext/vaultcontext-demo:base-golang-1.27.1-trixie@sha256:a4d1d139d0b0e7313de2fbe7cf4e27e3b934c164c5c58b33af44af2e9ba2fc4f AS build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src/minio
# RELEASE.2025-10-15T17-29-55Z (official minio/minio source).
RUN git init -q . \
    && git remote add origin https://github.com/minio/minio.git \
    && git fetch -q --depth 1 origin 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
    && git checkout -q --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
    && go mod download && go mod verify \
    && go build -trimpath -ldflags '-s -w' -o /out/minio .
WORKDIR /src/mc
# RELEASE.2025-08-13T08-35-41Z (official minio/mc source).
RUN git init -q . \
    && git remote add origin https://github.com/minio/mc.git \
    && git fetch -q --depth 1 origin 7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && git checkout -q --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && go mod download && go mod verify \
    && go build -trimpath -ldflags '-s -w' -o /out/mc .

FROM ghcr.io/pocketcontext/vaultcontext-demo:base-debian-trixie-20260918-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
COPY --from=build /out/minio /out/mc /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/minio"]
