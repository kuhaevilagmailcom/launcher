FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# БД живёт в /app/data — монтируй volume, чтобы не потерять профили и опыт
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "main.py"]
