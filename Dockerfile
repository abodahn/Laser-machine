# Mirror image only. The collector runs as a Windows service on the factory PC and is
# never containerised — it needs SMB to 10.100.x.x, which no cloud host can reach.
#
# The build context is platform/. That is load-bearing: ../machines.json (26 factory IPs)
# and ../central.db live one level ABOVE it and therefore cannot be copied in by accident.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# The factory writes every timestamp in Africa/Cairo local time and every read path
# resolves "today" with date.today(). A UTC container is 2-3 h behind Cairo, so it
# would serve yesterday as today from 00:00 to 03:00 Cairo -- the tail of the 22:00-06:00
# night shift, and the hours the eod forecast lookup would find nothing at all.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=Africa/Cairo

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY web/ web/
COPY DATA_DICTIONARY.json .

ENV LASER_ROLE=mirror LASER_DATA=/data PORT=8000
# Ephemeral by design: the collector refills it in one push cycle after any wipe.
RUN mkdir -p /data

# Shell form so $PORT expands (Render assigns it).
CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT
