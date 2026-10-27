FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install -e . && useradd --create-home --uid 10001 app
COPY migrations ./migrations
COPY persona ./persona

USER app
CMD ["python", "-m", "companion"]
