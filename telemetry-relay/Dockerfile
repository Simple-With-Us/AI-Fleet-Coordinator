# telemetry-relay/Dockerfile — fleet-telemetry-relay image.
#
# Pinned otelcol-contrib 0.162.0 (latest stable 2026-09-29).  Bump OTELCOL_VERSION
# to a published stable tag from
# https://hub.docker.com/r/otel/opentelemetry-collector-contrib/tags (never a
# nightly).  No secrets are baked in:  every credential arrives as env at run
# time (see COOLIFY.md).
ARG OTELCOL_VERSION=0.162.0
FROM ghcr.io/open-telemetry/opentelemetry-collector-contrib:${OTELCOL_VERSION}

COPY otelcol.yaml /etc/otelcol/config.yaml

# 4318 Claude Code, 4319 Codex, 4320 Grok CLI, 4321 hook shim, 13133 health.
EXPOSE 4318 4319 4320 4321 13133

ENTRYPOINT ["/otelcol-contrib"]
CMD ["--config=/etc/otelcol/config.yaml"]
