FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py wardrobe_client.py ./

# Nothing here needs root, and the users file is mounted read-only.
RUN useradd -m -u 1000 appuser
USER appuser

ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8090 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4).status == 200 else 1)"

CMD ["python", "server.py"]
