# Container image for cloud runs (Cloudflare Containers, GitHub Actions, any VPS).
# The container runs one scrape and exits; schedule it externally at 01:00 UTC (= 07:00 Dhaka).
# Mount a volume at /app/data to keep results between runs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Dhaka
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config

ENTRYPOINT ["avn"]
CMD ["run"]
