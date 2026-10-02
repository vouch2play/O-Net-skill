FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the code goes in the image. onet.db (~200MB) is NOT baked in here —
# it lives on a mounted persistent volume (see fly.toml / ONET_DB_PATH),
# so redeploys stay fast and you don't ship a fresh 200MB layer every push.
COPY onet_hosted_server.py onet_search.py ./

ENV ONET_DB_PATH=/data/onet.db
ENV ONET_USAGE_DB=/data/usage.db
ENV PORT=8000

EXPOSE 8000

CMD ["python", "onet_hosted_server.py"]
