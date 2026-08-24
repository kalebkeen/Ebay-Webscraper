# Deploy once, then the phone just opens a URL forever -- no laptop running.
# Works as-is on Render, Railway, Fly.io, or any container host.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Barcode map lives on a mounted volume so it survives redeploys.
ENV PS2ARB_UPC_STORE=/data/upc_map.json
VOLUME /data
EXPOSE 8000
CMD ["sh","-c","uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
