FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --uid 10001 --create-home trader && mkdir /app/data && chown trader /app/data
COPY config.toml dashboard.py ./
USER trader
CMD ["smart-trader", "run"]
