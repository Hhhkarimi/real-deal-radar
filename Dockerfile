FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home radar
USER radar
WORKDIR /work
ENTRYPOINT ["dealradar"]
CMD ["watch", "--config", "/work/config.toml"]
