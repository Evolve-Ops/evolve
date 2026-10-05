FROM python:3.12-slim
COPY . /app
CMD ["celery", "-A", "app", "worker"]
