FROM python:3.12-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY blink2telegram.py web_dashboard.py entrypoint.py ./

# Dirs for persisted data
RUN mkdir -p /app/clips /app/data

# Web dashboard port
EXPOSE 8080

# Persistent volumes for config, credentials, clips, db
VOLUME ["/app/data"]

CMD ["python", "-u", "entrypoint.py"]
