# Voice -> Note service image. PDF output needs Chromium + Playwright (included); HTML output needs neither.
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg chromium nodejs npm libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 libdrm2 \
      libgbm1 libgtk-3-0 libnspr4 libnss3 libu2f-udev libvulkan1 libxcomposite1 libxdamage1 libxfixes3 \
      libxkbcommon0 libxrandr2 xdg-utils \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --prefix /opt/playwright playwright@1.54.2
COPY requirements.txt constraints.txt /app/
RUN pip install -r requirements.txt -c constraints.txt
COPY service /app/service
WORKDIR /app/service
ENV DJANGO_SETTINGS_MODULE=config.settings.service VNS_DATA_DIR=/data
# Run migrations once, then your own worker code; example: docker run ... python -m voice_notes process /in/a.m4a --out /out/a
CMD ["python", "-m", "voice_notes", "selftest"]
