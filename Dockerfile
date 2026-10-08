FROM python:3.13-slim

# Standard library only: nothing to pip install.
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY deluge_exporter.py .

# Run as an unprivileged user; the exporter needs no filesystem access.
USER 65534:65534

ENTRYPOINT ["python", "/app/deluge_exporter.py"]
