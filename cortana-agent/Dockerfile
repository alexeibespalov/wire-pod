FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# Built from the folder that holds both agents and agent-core (see compose.yaml: context "."),
# because the shared brain lives in ../agent-core.
# yt-dlp runs YouTube's player script in Deno to work out audio URLs (agent_core/ytmusic.py)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
COPY cortana-agent/requirements.txt .
RUN pip install -r requirements.txt
COPY agent-core /srv/agent-core
RUN pip install --no-deps /srv/agent-core && pip freeze > /srv/requirements.lock

COPY cortana-agent/app ./app
COPY cortana-agent/skills ./skills
COPY cortana-agent/tests ./tests

# runs as non-root; /data is the agent's state volume (threads, memories, reminders, logs)
ENV HOME=/home/agent
RUN useradd -r -u 10002 -d /home/agent agent && mkdir -p /data /home/agent && chown agent /data /home/agent
USER agent

CMD ["python", "-m", "app.server"]
