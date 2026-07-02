# Deployment Guide (Docker + GHCR)

This guide covers deploying Webstead with the published GHCR image and a Docker Compose stack.

> Webstead is alpha software. Expect breaking changes and data loss risks.

---

## Prerequisites

- Docker + Docker Compose
- A domain name (if deploying publicly)
- A reverse proxy with TLS (Caddy, Nginx, Traefik, etc.)

---

## Recommended hardware

For a personal site, a cheap VPS is sufficient. The full stack (web + Celery worker + beat + Postgres + Redis + MinIO) fits comfortably in 1–2 GB of RAM.

**Recommended: [Hetzner CAX11](https://www.hetzner.com/cloud/)** (~€4/mo)
- 2 ARM vCPU, 4 GB RAM, 40 GB SSD
- Plenty of headroom for the full stack with room to grow

Any provider works. If you prefer a more managed experience and less to operate yourself, DigitalOcean App Platform with managed Postgres and Valkey (Redis) is a solid option — easier to run but costs more.

---

## 1) Create a deployment directory

Create a fresh folder (not this repo) for your deployment assets:

```
webstead-deploy/
  docker-compose.yml
  .env
```

Copy `docs/docker-compose.example.yml` from this repo as your starting point.

---

## 2) Create a `.env` file

Use this as a starting point. Update passwords and hostnames.

```env
# Core
DEBUG=False
SECRET_KEY="change-me-to-a-long-random-string"

# Hosts (required when DEBUG=False)
ALLOWED_HOSTS=example.com,.example.com
CSRF_TRUSTED_ORIGINS=https://example.com

# Database (points at the compose service name)
DB_NAME=webstead
DB_USER=webstead
DB_PASS=webstead
DB_HOST=postgres
DB_PORT=5432

# Redis / Celery broker
CELERY_BROKER_URL=redis://redis:6379/0

# S3/MinIO storage
AWS_ACCESS_KEY_ID=minio
AWS_SECRET_ACCESS_KEY=minio12345
AWS_STORAGE_BUCKET_NAME=webstead
# For public deployments, set this to the public URL for your S3/MinIO endpoint.
AWS_S3_ENDPOINT_URL=http://minio:9000
AWS_S3_REGION_NAME=us-east-1
# Optional (use a CDN or public domain for bucket access)
# AWS_S3_CUSTOM_DOMAIN=cdn.example.com

# Optional services
AKISMET_API_KEY=
TURNSTILE_SITE_KEY=
TURNSTILE_SECRET_KEY=
```

Notes:

- `SECRET_KEY` must be unique and kept private.
- `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` are required when `DEBUG=False`.
- The MinIO bucket must exist before `collectstatic` runs (Webstead creates it automatically on startup).
- `AWS_S3_ENDPOINT_URL` is also used in public media URLs; set it to a public domain if you expect users to load assets from the internet.

---

## 3) Create `docker-compose.yml`

Start from `docs/docker-compose.example.yml`. It runs the full stack:

| Service | Purpose |
|---|---|
| `postgres` | Primary database |
| `redis` | Celery broker |
| `minio` | S3-compatible object storage for media and static files |
| `webstead` | Django/Gunicorn web server |
| `celery-worker` | Async task worker (webmentions, feed polling, etc.) |
| `celery-beat` | Periodic task scheduler |

The `celery-worker` and `celery-beat` services start after `webstead` passes its health check, so migrations are guaranteed to be complete before workers come up.

---

## 4) Start the stack

From your deployment directory:

```
docker compose up -d
```

Health checks:

- The container binds to `PORT` (falling back to `8000`).
- Use `/healthz` as the HTTP health check path for a lightweight readiness probe.

---

## 5) Automatic bucket creation + collectstatic

On container startup, Webstead will:

- wait for the database
- run migrations
- create the S3/MinIO bucket (if missing)
- apply a public-read bucket policy (optional)
- run `collectstatic`

You can tune this behavior via environment variables:

```env
# Startup behavior (defaults shown)
DB_WAIT=true
DB_WAIT_TIMEOUT=60
DB_WAIT_INTERVAL=2
COLLECTSTATIC=true
STORAGE_BOOTSTRAP=true
STORAGE_BOOTSTRAP_BUCKET=true
STORAGE_BOOTSTRAP_POLICY=true
STORAGE_BUCKET_PUBLIC_READ=true
```

---

## 6) Create a Django superuser

```
docker compose exec webstead uv run manage.py createsuperuser
```

Log in at `http://<your-host>/admin`.

---

## Reverse proxy + TLS example

Webstead expects `X-Forwarded-Proto` and `X-Forwarded-Host` to be set correctly by your proxy.

### Caddy (recommended)

```
example.com {
  encode gzip zstd
  reverse_proxy webstead:8000
}
```

### Nginx

```
server {
  listen 80;
  server_name example.com;

  location / {
    proxy_pass http://webstead:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
}
```

When running behind a proxy, set:

- `ALLOWED_HOSTS=example.com,.example.com`
- `CSRF_TRUSTED_ORIGINS=https://example.com`

---

## Configuration reference

Required in production:

- `DEBUG=False`
- `SECRET_KEY`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `DB_NAME`, `DB_USER`, `DB_PASS`, `DB_HOST`, `DB_PORT`
- `CELERY_BROKER_URL`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_S3_REGION_NAME`

Optional:

- `AWS_S3_CUSTOM_DOMAIN`
- `AKISMET_API_KEY`
- `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`

---

## Deployment readiness notes

As of this guide, the container startup flow handles DB wait, migrations, bucket bootstrap, and `collectstatic`. If you need to disable any of those, use the startup environment variables in step 5.
