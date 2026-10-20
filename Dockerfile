FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY schema ./schema
COPY data ./data
RUN pip install --no-cache-dir .
EXPOSE 8000
ENV GLOBAL_EXECUTABLES_ROOT=/app
CMD ["global-executables-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
