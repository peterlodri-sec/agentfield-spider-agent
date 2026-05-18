FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Docling is opt-in (~1.5 GB transformers+torch on first model
# download). To enable:
#   docker build --build-arg INSTALL_DOCLING=1 .
# Models cache under HF cache; mount a volume in prod or expect a
# one-time download on the first PDF / Word request.
#
# Lightpanda support is included by default (just the Playwright
# Python client; ~10 MB). The Lightpanda BINARY runs in a sibling
# container — see Dockerfile.lightpanda + docker-compose.yml.
ARG INSTALL_DOCLING=0

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "${INSTALL_DOCLING}" = "1" ]; then \
        pip install --no-cache-dir "docling>=2.0"; \
    fi

COPY main.py spider_server.py ./
COPY reasoners ./reasoners
COPY spider ./spider

EXPOSE 8005 11700

# Explicit STOPSIGNAL — docker defaults to SIGTERM but stating it lets
# operators (and future Dockerfile readers) know we rely on it. Python
# isn't PID 1 in some orchestrators; if you wrap with tini/dumb-init,
# they forward SIGTERM correctly.
STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
