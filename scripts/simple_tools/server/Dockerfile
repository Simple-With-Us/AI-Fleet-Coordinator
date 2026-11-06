# Operator listener, server instance:  the Grok Bot (GB) personas, woken through their Grok Bot
# routine webhooks.  The Mac instance (the CLAUDE seat) is the LaunchAgent; a seat is never held by
# both (docs/protocols/simple-tools-partition.toml).  Design and Coolify setup:
# docs/protocols/eavesdropper-server.md.
#
# Build context is the repo root:
#   docker build -f scripts/simple_tools/server/Dockerfile -t simple-tools-server .
#
# No secrets are baked in.  Bot emails and keys and the routine URLs and keys arrive at run time:
# loaded from Infisical by the entrypoint when Coolify binds the scoped INFISICAL_TOKEN
# service token (server/infisical_env.py).  ZULIP_SITE is a Coolify
# variable either way.  State, the config and the logs live on the /data volume.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agentsync \
    AGENT_SYNC_INSTANCE=server \
    AGENT_SYNC_STATE_DIR=/data \
    AGENT_SYNC_CONFIG=/data/listener.toml \
    AGENT_SYNC_LOG_STDOUT=1

# Optional native Zulip outgoing-webhook receiver (8787) and the signed listener read API (8788, AFC-330);
# each is disabled unless listener.toml opts in.
EXPOSE 8787 8788

RUN groupadd --system --gid 10001 agentsync \
 && useradd --system --uid 10001 --gid agentsync --home-dir /home/agentsync --create-home \
        --shell /usr/sbin/nologin agentsync \
 && mkdir -p /data \
 && chown agentsync:agentsync /data \
 && chmod 0700 /data

WORKDIR /app
# The entry script and the package must be siblings:  the script puts its own directory on sys.path.
COPY scripts/simple-chat /app/scripts/simple-chat
COPY scripts/simple_tools /app/scripts/simple_tools
# The Operator's Linear project webhook (AFC-565) runs these two with the listener.
COPY scripts/linear_project_linkifiers.py scripts/zulip_linkifiers.py /app/scripts/
# The seat partition, read by the daemon at start (it refuses a seat of the Mac instance).
COPY docs/protocols/simple-tools-partition.toml /app/docs/protocols/simple-tools-partition.toml
# The seat registry, read by identity.strict_seat and seat_app (no secrets in it:  tags, bot emails, app names).
COPY fleet-apps.json /app/fleet-apps.json
RUN chmod 0755 /app/scripts/simple-chat /app/scripts/simple_tools/server/entrypoint.sh \
        /app/scripts/simple_tools/server/healthcheck.py /app/scripts/simple_tools/server/infisical_env.py \
        /app/scripts/simple_tools/server/apply_live_config.py \
 && ln -s /app/scripts/simple-chat /usr/local/bin/simple-chat

USER agentsync

# Healthy while the daemon rewrites /data/listener/status.json (every 2 seconds); unhealthy when
# the file is older than 60 seconds or missing (a refused start never writes it).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "/app/scripts/simple_tools/server/healthcheck.py"]

# Exec form all the way down (the entry script execs python), so SIGTERM reaches the daemon and it
# deletes its event queues on the way out.
ENTRYPOINT ["/app/scripts/simple_tools/server/entrypoint.sh"]
CMD ["daemon", "run", "--wait-lock"]
