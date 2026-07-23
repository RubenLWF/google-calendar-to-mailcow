FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY filter_gcal_ics.py sync_all.py ./

# Paths point at the mounted volumes; STATUS_DIR must be a persistent volume so
# the vdirsyncer cache survives restarts (otherwise every restart re-syncs all).
ENV PEOPLE_DIR=/config/people \
    STATUS_DIR=/data/status \
    FILTERED_DIR=/data/filtered \
    CONFIG_PATH=/tmp/vdirsyncer.config \
    SYNC_INTERVAL=900 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]

CMD ["python3", "sync_all.py"]
