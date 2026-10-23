FROM python:3.12.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ALFA_PROJECT_DIR=/app PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
COPY data ./data
COPY deploy ./deploy
RUN python -m alfa_agent build-case-index
RUN mkdir -p /app/var && chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "alfa_agent", "serve"]
