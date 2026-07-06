FROM python:3.12-slim

# smartmontools — SMART-мониторинг дисков (/smart);
# iputils-ping — ICMP-проверка AWG-туннеля
RUN apt-get update \
    && apt-get install -y --no-install-recommends smartmontools iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]
