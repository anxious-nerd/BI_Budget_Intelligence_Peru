# Imagen del ETL (DuckDB) + cargador a SQL Server. La usa el servicio `etl` del docker-compose.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "etl.load_mssql", "--help"]
