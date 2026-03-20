FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ha_agent/ ha_agent/

CMD ["python", "-m", "ha_agent.telegram_bot"]
