FROM python:3.14.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY exporter.py queries.json /app/

USER 65534:65534
EXPOSE 9464
ENTRYPOINT ["python", "/app/exporter.py"]
