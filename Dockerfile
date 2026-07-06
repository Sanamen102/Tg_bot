FROM python:3.12-slim

# smartmontools — для SMART-мониторинга дисков (/smart)
RUN apt-get update \
    && apt-get install -y --no-install-recommends smartmontools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]
