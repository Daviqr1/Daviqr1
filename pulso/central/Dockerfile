FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PULSO_DB=/data/pulso.sqlite PULSO_ARCHIVE=/data/fit-archive
WORKDIR /app
COPY pyproject.toml README.md ./
COPY pulso ./pulso
RUN pip install --no-cache-dir ".[analysis,garmin,ai,calendar]" && useradd -r -u 10001 pulso && mkdir -p /data && chown pulso /data
USER pulso
VOLUME ["/data"]
EXPOSE 8080
CMD ["pulso", "serve", "--host", "0.0.0.0", "--port", "8080"]
