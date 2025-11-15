# Dockerfile
FROM python:3.14-alpine

# Python runtime tweaks
ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Install uv once, no pip cache left behind
RUN pip install --no-cache-dir uv

# Copy project metadata first to leverage layer caching
COPY pyproject.toml uv.lock* ./

# Install only prod deps into a local .venv
RUN uv sync --no-dev --group prod && \
  rm -rf /root/.cache

# Copy the rest of your app
COPY . .

# Non root user
RUN addgroup -S app && adduser -S -G app app
USER app

EXPOSE 8000

# Run migrations on every start, then launch gunicorn
CMD ["sh", "-c", "uv run manage.py migrate && gunicorn config.wsgi:application -b 0.0.0.0:8000"]
