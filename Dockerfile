FROM python:3.13-slim

# Standard library only: nothing to pip install.
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY deluge_exporter.py .

# Run as an unprivileged user; the exporter needs no filesystem access.
USER 65534:65534

# Healthy while the exporter has written successfully within the last 3 x INTERVAL seconds.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=2 \
  CMD ["python", "/app/deluge_exporter.py", "--healthcheck"]

ENTRYPOINT ["python", "/app/deluge_exporter.py"]
