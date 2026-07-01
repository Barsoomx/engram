FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV POETRY_VERSION=1.8.2

WORKDIR /srv/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && pip install --no-cache-dir "poetry==$POETRY_VERSION" \
    && rm -rf /var/lib/apt/lists/*

COPY apps/backend/pyproject.toml apps/backend/poetry.lock ./

RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-root --only main

COPY apps/backend /srv/app

CMD ["gunicorn", "settings.wsgi:application", "--bind", "0.0.0.0:8000"]
