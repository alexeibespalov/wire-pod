FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt && pip freeze > /srv/requirements.lock

COPY app ./app

# runs as non-root; /data is the agent's state volume (threads, memories)
RUN useradd -r -u 10001 agent && mkdir -p /data && chown agent /data
USER agent

CMD ["python", "-m", "app.server"]
