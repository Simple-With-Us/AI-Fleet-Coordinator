# fleet-recall-service -- COPY-based image for anyone who prefers a built image over bootstrap.sh.
#
#   docker build -t fleet-recall-service -f scripts/fleet-recall-service/Dockerfile scripts
#   docker run --rm -p 8080:8080 -e RECALL_API_TOKEN=... -e QDRANT_URL=... (see README.md) fleet-recall-service
#
# Build context is scripts/ so both the package and the service dir are copied.  Stdlib only:
# no pip install step.  The one binary added is gitleaks (pinned via GITLEAKS_VERSION), so the
# contribute gate in the container matches the Mac seats; the build fails if it cannot be
# installed (override with --build-arg RECALL_GITLEAKS_REQUIRED=0 to tolerate that).
FROM python:3.14-slim

ARG GITLEAKS_VERSION=8.30.1
ARG RECALL_GITLEAKS_REQUIRED=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY fleet_rag /app/fleet_rag
COPY fleet-recall-service /app/fleet-recall-service

# Same installer as the Coolify bootstrap path (RECALL_GITLEAKS_ONLY=1 runs only that step):
# pinned release tarball for `uname -m`, size check, `gitleaks version` must print the pin,
# then /usr/local/bin/gitleaks.
RUN RECALL_GITLEAKS_ONLY=1 GITLEAKS_VERSION="${GITLEAKS_VERSION}" \
    RECALL_GITLEAKS_REQUIRED="${RECALL_GITLEAKS_REQUIRED}" \
    bash /app/fleet-recall-service/bootstrap.sh

RUN useradd --system --uid 10001 --no-create-home recall \
 && rm -rf /app/fleet_rag/__pycache__ /app/fleet_rag/tests/__pycache__
USER recall

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python3 -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'),timeout=5).status==200 else 1)"

CMD ["python3", "/app/fleet-recall-service/server.py"]
