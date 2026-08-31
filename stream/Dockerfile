FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy pillow

WORKDIR /app
COPY battle_stream.py .

CMD ["python3", "battle_stream.py", "--key-file", "/run/key.txt"]
