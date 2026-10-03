FROM python:3.12-slim

RUN pip install --no-cache-dir "pydeno>=0.5" \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin hack

WORKDIR /app
COPY server.py /app/server.py

USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SECRET_FILE=/run/hack/secret.txt HACK_LOG=/var/log/hack/requests.jsonl
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"
CMD ["python", "/app/server.py", "--host", "0.0.0.0", "--port", "8080", "--trust-proxy"]
