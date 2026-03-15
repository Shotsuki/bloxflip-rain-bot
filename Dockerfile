FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bloxflip_rain_notifier.py .

CMD ["python", "bloxflip_rain_notifier.py"]
