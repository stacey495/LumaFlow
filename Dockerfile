FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "Pillow>=12.2,<14"
COPY lumaflow /app/lumaflow
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "lumaflow.worker", "serve"]
