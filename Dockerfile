FROM python:3.13-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.3.8" && useradd --uid 10001 --no-create-home app
COPY server.py /app/server.py
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "/app/server.py"]
