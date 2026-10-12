# One image, two roles (api | worker) selected by the command - keeps API and
# worker code/versions in lock-step and halves the build/scan surface.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

# Untrusted student files are handled by this image: never run as root.
RUN useradd --uid 10001 --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser
ENV STORAGE_DIR=/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "2"]
