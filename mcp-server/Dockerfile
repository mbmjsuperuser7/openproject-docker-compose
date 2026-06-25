FROM python:3.12-slim

# Non-root user
RUN useradd -m -u 1001 mcpuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Drop to non-root
USER mcpuser

EXPOSE 3000
CMD ["python", "server.py"]
