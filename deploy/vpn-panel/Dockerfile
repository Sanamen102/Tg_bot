FROM python:3.12-alpine

# openssh-client — панель ходит на VPS тем же forced-command ключом, что и бот.
RUN apk add --no-cache openssh-client \
 && pip install --no-cache-dir flask==3.0.3 segno==1.6.1 gunicorn==22.0.0

WORKDIR /app
COPY app.py .
COPY templates ./templates

# Один воркер: панель обслуживает одного человека, а состояние блокировок
# перебора живёт в памяти процесса и между воркерами не разделяется.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8091", \
     "--access-logfile", "-", "--error-logfile", "-", "app:app"]
