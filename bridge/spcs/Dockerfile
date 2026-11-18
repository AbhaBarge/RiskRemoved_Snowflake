# Risk Copilot Slack bot for Snowpark Container Services.
# Build from the repo root (the bot imports ../src and reads ../conf):
#   docker build --platform linux/amd64 -f bridge/spcs/Dockerfile -t risk-copilot-slack-bot:latest .
# No secrets go into the image: .env and keys/ are excluded by bridge/spcs/Dockerfile.dockerignore.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RC_BRIDGE_ENV=/app/bridge/.env.absent

WORKDIR /app
COPY bridge/requirements.txt bridge/requirements.txt
RUN pip install --no-cache-dir -r bridge/requirements.txt

COPY src/ src/
COPY conf/ conf/
COPY bridge/slack_bot.py bridge/analyst_client.py bridge/
COPY bridge/spcs/entrypoint.py bridge/spcs/entrypoint.py

RUN useradd --create-home bot
USER bot

CMD ["python", "/app/bridge/spcs/entrypoint.py"]
