FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-color-emoji libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt && pip freeze > /srv/requirements.lock

COPY app ./app
COPY skills ./skills

# runs as non-root; /data is the agent's state volume (threads, memories)
# HOME holds .anki_vector/ (SDK config + cert, bind-mounted read-only) for the face watcher
ENV HOME=/home/agent
RUN useradd -r -u 10001 -d /home/agent agent && mkdir -p /data /home/agent && chown agent /data /home/agent
USER agent

CMD ["python", "-m", "app.server"]
