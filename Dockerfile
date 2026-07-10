FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY assets ./assets

ENV COMPLETE_DIR=/downloads/complete \
    INCOMPLETE_DIR=/downloads/incomplete \
    STATE_FILE=/config/state.json

VOLUME ["/downloads", "/config"]
EXPOSE 9797

HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9797/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9797"]
