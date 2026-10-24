# Build from the repository root:
#   docker build -f portal/Dockerfile -t netdevops-portal .
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY portal/requirements.txt ./portal/requirements.txt
RUN pip install --no-cache-dir -r portal/requirements.txt

# The portal reads the intent and reuses the same validate/render code
# the pipeline runs, so it needs those alongside its own source.
COPY intent/    ./intent/
COPY templates/ ./templates/
COPY scripts/   ./scripts/
COPY lab/       ./lab/
COPY configs/   ./configs/
COPY portal/    ./portal/

RUN useradd --create-home --uid 10001 portal && chown -R portal /app
USER portal

EXPOSE 8080
CMD ["uvicorn", "portal.app:app", "--host", "0.0.0.0", "--port", "8080"]
