FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gatekeeper_agent.py .

EXPOSE 8000

CMD ["python", "gatekeeper_agent.py"]
