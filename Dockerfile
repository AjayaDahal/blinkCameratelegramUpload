FROM python:3.12-slim

WORKDIR /app

# System deps for OpenCV & fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY blink2telegram.py web_dashboard.py entrypoint.py ai_processor.py dashboard.html ./

# Dirs for persisted data
RUN mkdir -p /app/clips /app/data

# Web dashboard port
EXPOSE 8080

# Persistent volumes for config, credentials, clips, db
VOLUME ["/app/data"]

CMD ["python", "-u", "entrypoint.py"]
