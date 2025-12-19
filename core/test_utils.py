import json
from pathlib import Path
from typing import Optional, Sequence, Tuple, Union


def build_test_theme(
    slug: str,
    base_dir: Union[str, Path],
    *,
    extra_files: Optional[Sequence[Tuple[str, str]]] = None,
    metadata: Optional[dict] = None,
) -> Path:
    """Create a minimal valid theme directory for tests."""
    theme_dir = Path(base_dir) / slug
    theme_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = theme_dir / "templates"
    static_dir = theme_dir / "static"
    templates_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)

    payload = {"label": "Test Theme", "slug": slug, "version": "1.0"}
    if metadata:
        payload.update(metadata)

    (theme_dir / "theme.json").write_text(json.dumps(payload))
    (templates_dir / "base.html").write_text("<!doctype html>")
    (static_dir / "style.css").write_text("body{}")

    if extra_files:
        for relative_path, content in extra_files:
            target = theme_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    return theme_dir
