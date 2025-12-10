FROM python:3.13-slim

# 1) Dipendenze di base
RUN apt-get update && apt-get install -y \
    wget \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# 2) FFmpeg statico aggiornato (release amd64)
WORKDIR /opt

RUN wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    && tar -xJf ffmpeg-release-amd64-static.tar.xz \
    && mv ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ffmpeg \
    && mv ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ffprobe \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf ffmpeg-*-amd64-static ffmpeg-release-amd64-static.tar.xz

# 3) Installa le dipendenze Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) Copia il codice dell'app
COPY . .

# 5) Avvia Flask con Gunicorn
# Supponiamo che in app.py tu abbia qualcosa tipo: app = Flask(__name__)
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT:-8000} app:app"]

