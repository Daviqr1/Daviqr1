FROM python:3.13-slim

# Everything the central writes lives on the /data volume: the database, the FIT archive, the summary
# files, the Google login and Doc ids (PULSO_HOME) and the Garmin Connect tokens. The container user
# also gets a real, writable HOME for libraries that cache under ~.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PULSO_HOME=/data PULSO_DB=/data/pulso.sqlite \
    PULSO_ARCHIVE=/data/fit-archive PULSO_GARMIN_TOKENS=/data/garminconnect
WORKDIR /app
COPY pyproject.toml README.md ./
COPY pulso ./pulso
# gdrive brings google-api-python-client, which both the Drive summary and the Calendar alert push need
RUN pip install --no-cache-dir ".[analysis,garmin,ai,calendar,gdrive]" \
    && useradd -r -m -u 10001 pulso \
    && mkdir -p /data && chown pulso:pulso /data && chmod 700 /data
USER pulso
VOLUME ["/data"]
EXPOSE 8080
CMD ["pulso", "serve", "--host", "0.0.0.0", "--port", "8080"]
