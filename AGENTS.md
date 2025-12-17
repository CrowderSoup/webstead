# Repository Guidelines

## Project Structure & Module Organization

- `core/`, `blog/`, `micropub/`, `analytics/`, and `files/` are Django apps. Shared project settings live in `config/`.
- Templates live under each app in `*/templates/`, with theme overrides in `themes/<slug>/templates/`.
- Static assets are in `static/` and theme assets in `themes/<slug>/static/`.
- Tests are colocated in each app’s `tests.py` (e.g., `core/tests.py`).
- Operational files: `manage.py`, `docker-compose.yml`, `Dockerfile`, `sample.env`.

## Build, Test, and Development Commands

- `uv run manage.py runserver` — start the local dev server.
- `uv run manage.py migrate` — apply database migrations.
- `uv run manage.py test` — run the Django test suite.
- `uv run manage.py collectstatic` — gather static + theme assets for serving.
- `docker-compose up -d` — start local Postgres/MinIO services for storage.

## Coding Style & Naming Conventions

- Follow standard Django/PEP 8 style with 4-space indentation.
- Classes use `PascalCase` (models, forms); functions/fields use `snake_case`.
- Keep model fields explicit about `null`/`blank` and `on_delete` behavior.
- Templates and static files should mirror app paths when overriding (`themes/<slug>/templates/blog/post.html`).

## Testing Guidelines

- Tests use Django’s `TestCase` in `*/tests.py`.
- Name test classes by feature (e.g., `PostModelTests`) and test methods with `test_` prefixes.
- Run all tests with `uv run manage.py test`; no repo-level coverage target is enforced.

## Commit & Pull Request Guidelines

- Recent history shows lightweight messages; prefer concise, imperative summaries (e.g., “Add site author field”).
- PRs should include: a short description, linked tickets/issues, and testing notes.
- Include screenshots for admin/UI changes and note any migration requirements.

## Configuration Notes

- Copy `sample.env` to your environment; it includes local DB and S3/MinIO settings.
- Theme discovery relies on `themes/` and requires `theme.json` per theme.
