FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PUID=99 PGID=100 UMASK=002
RUN apt-get update && apt-get install -y --no-install-recommends gosu tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 openreads \
    && useradd -u 1000 -g 1000 -d /app -s /usr/sbin/nologin openreads
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY static ./static
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh && mkdir -p /data
RUN chown openreads:openreads /data
# Unprivileged from the start (trivy DS-0002); compose sets `user: PUID:PGID`.
USER openreads
VOLUME /data
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health',timeout=3)" || exit 1
ENTRYPOINT ["/usr/bin/tini","--","/entrypoint.sh"]
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8790"]
