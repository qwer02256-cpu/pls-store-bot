FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV DB_PATH=/app/data/store.db
ENV PLATEGA_CALLBACK_PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
