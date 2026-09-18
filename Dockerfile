# Adapted from maximhq/bifrost transports/Dockerfile at upstream.env's revision.
# Modifications: dynamic ELF linking, matching Alpine runtime series, build
# provenance/license files, and a separate CI-only plugin test target.
# Pass the pinned image arguments from upstream.env (make build does this).
ARG NODE_IMAGE
ARG GO_IMAGE
ARG RUNTIME_IMAGE

FROM ${NODE_IMAGE} AS ui
WORKDIR /src/ui
COPY .upstream/ui/package*.json ./
RUN apk upgrade --no-cache && npm ci
COPY .upstream/ui/ ./
# This is the same UI build command used by the upstream OSS Dockerfile.
RUN npm run build-enterprise
COPY scripts/collect-licenses.sh /collect-licenses.sh
RUN sh /collect-licenses.sh /src/ui/node_modules /ui-licenses

FROM ${GO_IMAGE} AS compiler
RUN apk upgrade --no-cache && apk add --no-cache gcc musl-dev sqlite-dev binutils binutils-gold git
ENV GOTOOLCHAIN=local GOWORK=off CGO_ENABLED=1 GOOS=linux GOARCH=amd64 GOAMD64=v1
WORKDIR /src/transports
ARG GO_VERSION
ARG BIFROST_VERSION
ARG BIFROST_COMMIT
ARG BUILD_TAGS
RUN test "$(go env GOVERSION)" = "go${GO_VERSION}"
COPY .upstream/transports/go.mod .upstream/transports/go.sum ./
RUN go mod download && go mod verify
COPY .upstream/transports/ ./
COPY --from=ui /src/ui/out ./bifrost-http/ui
# Retain sqlite_static for existing-plugin compatibility, but do NOT link libc
# with -static. The ELF interpreter check below enforces the distinction.
RUN mkdir -p /out/build && \
    go build -mod=readonly -buildvcs=false -trimpath -tags="${BUILD_TAGS}" \
      -ldflags="-w -s -linkmode=external -X main.Version=${BIFROST_VERSION}" \
      -o /out/bifrost ./bifrost-http && \
    readelf -l /out/bifrost > /out/build/elf.txt && \
    grep -q INTERP /out/build/elf.txt && \
    go version -m /out/bifrost > /out/build/go-buildinfo.txt && \
    go list -m -json all > /out/build/go-modules.json && \
    cp go.mod go.sum /out/build/ && \
    cp docker-entrypoint.sh /out/docker-entrypoint.sh && \
    apk info -v > /out/build/compiler-apk.txt && \
    printf '%s\n' "${BIFROST_COMMIT}" > /out/build/upstream-commit.txt
COPY upstream.env /out/build/upstream.env
COPY .upstream/ui/package-lock.json /out/build/ui-package-lock.json
COPY .upstream-notices/ /out/licenses/bifrost/
COPY LICENSE NOTICE /out/licenses/bifrost-dynamic/
COPY --from=ui /ui-licenses /out/licenses/npm
COPY scripts/collect-licenses.sh /collect-licenses.sh
RUN sh /collect-licenses.sh /go/pkg/mod /out/licenses/go && \
    cp "$(go list -m -f '{{.Dir}}' github.com/maximhq/bifrost/framework)/migrator/migrator.go" \
       /out/licenses/bifrost/migrator-source.go && \
    mkdir -p /out/licenses/mpl-sources && \
    for module in github.com/cyphar/filepath-securejoin github.com/hashicorp/go-version; do \
      name=$(basename "$module"); \
      find "/go/pkg/mod/cache/download/$module/@v" -maxdepth 1 -name '*.zip' \
        -exec sh -c 'cp "$1" "/out/licenses/mpl-sources/$2-$(basename "$1")"' sh '{}' "$name" \; ; \
    done && \
    test "$(find /out/licenses/mpl-sources -name '*.zip' | wc -l)" -ge 2

# Build the probe using the host's actual module graph and compiler environment.
# It is exported for CI, never installed in the final runtime image.
FROM compiler AS probe
COPY tests/plugin/main.go ./_image_smoke/main.go
RUN go build -mod=readonly -buildvcs=false -trimpath -tags="${BUILD_TAGS}" \
    -buildmode=plugin -o /out/smoke.so ./_image_smoke

FROM scratch AS testkit
COPY --from=probe /out/smoke.so /smoke.so
COPY --from=probe /out/build /build

FROM ${RUNTIME_IMAGE} AS runtime
WORKDIR /app
# Alpine's musl loader is already in the base. No compiler or Go SDK is shipped.
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates libgcc zlib && \
    adduser -D -u 1000 -s /bin/sh appuser && \
    mkdir -p /app/data/logs && chown -R 1000:0 /app/data && \
    chmod -R g=rwX /app/data
COPY --from=compiler --chmod=755 /out/bifrost /app/main
COPY --from=compiler --chmod=755 /out/docker-entrypoint.sh /app/docker-entrypoint.sh
COPY --from=compiler /out/build /usr/share/bifrost-build
COPY --from=compiler /out/licenses /usr/share/licenses
RUN apk info -v > /usr/share/bifrost-build/runtime-apk.txt && \
    cp /etc/apk/repositories /usr/share/bifrost-build/alpine-repositories.txt
ARG BUILD_REPOSITORY=https://github.com/fipnooone/bifrost-dynamic
ARG BUILD_REVISION=local
ARG IMAGE_VERSION=local
ARG BIFROST_VERSION
ARG BIFROST_COMMIT
LABEL org.opencontainers.image.title="Bifrost dynamic (unofficial)" \
      org.opencontainers.image.description="Unofficial dynamically linked Maxim Bifrost build for native Go plugins" \
      org.opencontainers.image.source="${BUILD_REPOSITORY}" \
      org.opencontainers.image.revision="${BUILD_REVISION}" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.bifrost-dynamic.upstream-version="${BIFROST_VERSION}" \
      io.bifrost-dynamic.upstream-revision="${BIFROST_COMMIT}"
ENV APP_PORT=8080 APP_HOST=0.0.0.0 APP_DIR=/app/data LOG_LEVEL=info LOG_STYLE=json \
    GOGC="" GOMEMLIMIT=""
USER 1000:0
VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${APP_PORT}/health" || exit 1
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["/app/main"]
