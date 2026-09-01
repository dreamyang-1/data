FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system agent && useradd --system --gid agent --create-home agent
WORKDIR /srv/app

COPY app ./app
COPY pyproject.toml ./
RUN pip install .

USER agent
EXPOSE 8088

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088", "--no-access-log"]
