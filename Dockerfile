# Dockerfile

# ---- Build the site_admin Tailwind CSS with the standalone CLI (no Node needed) ----
FROM alpine:3.20 AS css-builder
ARG TARGETARCH
RUN apk add --no-cache curl libstdc++ libgcc
WORKDIR /build
COPY site_admin/tailwind ./site_admin/tailwind
COPY site_admin/templates/site_admin ./site_admin/templates/site_admin
RUN set -eu; \
  case "${TARGETARCH}" in \
    amd64) TW_ARCH=x64; TW_SHA256=a04d34ceacc8f52cbe8920ad846cdeb61d3d0021dba32db0d1f77c9d9fad7a6c ;; \
    arm64) TW_ARCH=arm64; TW_SHA256=71ea4be79c9de9827545682df3e040053fb535d37c71ed2cfdedf9385a0868e0 ;; \
    *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
  esac; \
  curl -fsSL -o /usr/local/bin/tailwindcss \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-${TW_ARCH}-musl"; \
  echo "${TW_SHA256}  /usr/local/bin/tailwindcss" | sha256sum -c -; \
  chmod +x /usr/local/bin/tailwindcss
RUN tailwindcss \
  -i site_admin/tailwind/input.css \
  -o site_admin/static/site_admin/css/admin.css \
  --minify

FROM python:3.14-alpine

# Python runtime tweaks
ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PATH="/app/.venv/bin:${PATH}" \
  PYTHONPATH="/app" \
  UV_NO_DEV=1

WORKDIR /app

# Add git (and ssh client if you need SSH URLs)
RUN apk add --no-cache git

# Install uv once, no pip cache left behind
RUN pip install --no-cache-dir uv

# Copy project metadata first to leverage layer caching
COPY pyproject.toml uv.lock* ./

# Install only prod deps into a local .venv
RUN uv sync --no-dev --group prod && \
  rm -rf /root/.cache

# Copy the rest of your app
COPY . .

# Always ship a freshly built admin CSS, regenerated from the current templates
COPY --from=css-builder /build/site_admin/static/site_admin/css/admin.css site_admin/static/site_admin/css/admin.css

# Non root user
RUN addgroup -S app && adduser -S -G app app && \
  mkdir -p /app/themes /app/staticfiles && \
  chown -R app:app /app
USER app

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["sh", "-c", "gunicorn config.wsgi:application -b 0.0.0.0:${PORT:-8000}"]
