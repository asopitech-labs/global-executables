FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY schema ./schema
ADD https://codeload.github.com/asopitech-labs/global-executables/tar.gz/refs/heads/dictionary /tmp/dictionary.tar.gz
RUN mkdir /dictionary \
    && tar -xzf /tmp/dictionary.tar.gz --strip-components=1 -C /dictionary \
    && rm /tmp/dictionary.tar.gz
RUN pip install --no-cache-dir .
EXPOSE 8000
ENV GLOBAL_EXECUTABLES_ROOT=/app \
    GLOBAL_EXECUTABLES_DATASET_ROOT=/dictionary
CMD ["global-executables-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--allowed-host", "mcp:8000", "--allowed-host", "127.0.0.1:8000", "--allowed-host", "localhost:8000", "--allowed-host", "0.0.0.0:8000"]
