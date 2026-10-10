FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 traffic \
    && useradd --uid 10001 --gid traffic --no-create-home --home-dir /nonexistent traffic \
    && mkdir -p /app/logs \
    && chown traffic:traffic /app/logs

COPY traffic.py config.example.yaml ./
COPY src/ ./src/

USER 10001:10001

# Reuse the script's KeyboardInterrupt cleanup when Docker stops the container.
STOPSIGNAL SIGINT

ENTRYPOINT ["python", "traffic.py"]
CMD ["--config", "/app/config.yaml"]
