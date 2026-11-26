FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=99 \
    PGID=100 \
    UMASK=002

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gosu tini ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 bookdoe \
    && useradd -u 1000 -g 1000 -d /app -s /usr/sbin/nologin bookdoe

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
# Build stamp (docker compose passes GIT_SHA / BUILD_TIME; a plain `docker build` says "dev").
# Declared after the dependency layer so a new commit never re-installs requirements.
# The app reads the env vars first and app/build_info.json as the fallback.
ARG GIT_SHA=dev
ARG BUILD_TIME=
ENV BOOKDOE_BUILD_SHA=${GIT_SHA} \
    BOOKDOE_BUILD_TIME=${BUILD_TIME}
# The app version comes from app/__init__.py at runtime; the file carries the stamp only.
RUN python -c "import json,os; json.dump({'sha': os.environ['BOOKDOE_BUILD_SHA'] or 'dev', 'built_at': os.environ['BOOKDOE_BUILD_TIME']}, open('app/build_info.json','w'))"
# `bookdoe setup|status|secrets-check|ensure-secret` inside the container.
RUN chmod 0755 /entrypoint.sh && mkdir -p /config /books /audiobooks /downloads /migrations /library/ebooks /library/audiobooks \
    && printf '#!/bin/sh\ncd /app && exec python -m app.cli "$@"\n' > /usr/local/bin/bookdoe && chmod 0755 /usr/local/bin/bookdoe

# Run as the unprivileged user from the start (trivy DS-0002). docker-compose.yml sets
# `user: PUID:PGID` to match the host owner of the mounted folders.
RUN chown -R bookdoe:bookdoe /config /app/app/build_info.json
USER bookdoe
VOLUME ["/config"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/readyz',timeout=3)" || exit 1
ENTRYPOINT ["/usr/bin/tini","--","/entrypoint.sh"]
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8787","--proxy-headers","--forwarded-allow-ips=*"]
