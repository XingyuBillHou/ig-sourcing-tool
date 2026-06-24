FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY fb_competitor_ad_app.py fb_competitor_ad_core.py .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY .streamlit/config.toml .streamlit/config.toml

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p temp_videos exports .streamlit

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8501/_stcore/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["streamlit", "run", "fb_competitor_ad_app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]
