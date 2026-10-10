# agent-sync listener, server instance:  the Grok Bot (GB) personas, woken through their Grok Bot
# routine webhooks.  The Mac instance (the CLAUDE seat) is the LaunchAgent; a seat is never held by
# both (docs/protocols/agent-sync-partition.toml).  Design and Coolify setup:
# docs/protocols/agent-sync-server.md.
#
# Build context is the repo root:
#   docker build -f scripts/agent_sync/server/Dockerfile -t agent-sync-server .
#
# No secrets are baked in.  Bot emails and keys and the routine URLs and keys arrive at run time:
# loaded from Infisical by the entrypoint when Coolify sets the INFISICAL_* machine identity
# (server/infisical_env.py), else as runtime-only Coolify variables.  ZULIP_SITE is a Coolify
# variable either way.  State, the config and the logs live on the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agentsync \
    AGENT_SYNC_INSTANCE=server \
    AGENT_SYNC_STATE_DIR=/data \
    AGENT_SYNC_CONFIG=/data/listener.toml \
    AGENT_SYNC_LOG_STDOUT=1

RUN groupadd --system --gid 10001 agentsync \
 && useradd --system --uid 10001 --gid agentsync --home-dir /home/agentsync --create-home \
        --shell /usr/sbin/nologin agentsync \
 && mkdir -p /data \
 && chown agentsync:agentsync /data \
 && chmod 0700 /data

WORKDIR /app
# The entry script and the package must be siblings:  the script puts its own directory on sys.path.
COPY scripts/agent-sync /app/scripts/agent-sync
COPY scripts/agent_sync /app/scripts/agent_sync
# The seat partition, read by the daemon at start (it refuses a seat of the Mac instance).
COPY docs/protocols/agent-sync-partition.toml /app/docs/protocols/agent-sync-partition.toml
RUN chmod 0755 /app/scripts/agent-sync /app/scripts/agent_sync/server/entrypoint.sh \
        /app/scripts/agent_sync/server/healthcheck.py /app/scripts/agent_sync/server/infisical_env.py \
        /app/scripts/agent_sync/server/apply_live_config.py \
 && ln -s /app/scripts/agent-sync /usr/local/bin/agent-sync

USER agentsync

# Healthy while the daemon rewrites /data/listener/status.json (every 2 seconds); unhealthy when
# the file is older than 60 seconds or missing (a refused start never writes it).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "/app/scripts/agent_sync/server/healthcheck.py"]

# Exec form all the way down (the entry script execs python), so SIGTERM reaches the daemon and it
# deletes its event queues on the way out.
ENTRYPOINT ["/app/scripts/agent_sync/server/entrypoint.sh"]
CMD ["daemon", "run", "--wait-lock"]
