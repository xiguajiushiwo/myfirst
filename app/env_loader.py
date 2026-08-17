from __future__ import annotations

import os
from pathlib import Path


def load_project_env(start: str | Path | None = None) -> None:
    directory = Path(start or __file__).resolve()
    if directory.is_file():
        directory = directory.parent
    for candidate in (directory, *directory.parents):
        env_path = candidate / ".env"
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if value[:1] in ('"', "'"):
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end > 0 else value[1:]
            else:
                for index, character in enumerate(value):
                    if character == "#" and (index == 0 or value[index - 1] in " \t"):
                        value = value[:index].rstrip()
                        break
            os.environ.setdefault(key, value)
        return
