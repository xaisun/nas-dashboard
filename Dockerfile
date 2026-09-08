FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        smartmontools util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./

ENV TZ=Asia/Shanghai \
    PORT=8904 \
    NODE_ID=NODE-001

EXPOSE 8899
CMD ["python", "app.py"]
