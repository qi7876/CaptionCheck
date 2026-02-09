from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExternalEditorConfig:
    command: list[str] | None = None


@dataclass(frozen=True)
class AppConfig:
    data_root: Path
    external_editor: ExternalEditorConfig


def _coerce_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = shlex.split(value)
        return parts if parts else None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    raise TypeError("external_editor.command must be a string or list of strings")


def load_config(config_path: Path | None = None) -> AppConfig:
    if config_path is None:
        config_path = Path("captioncheck_config.json")

    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        raw = {}

    data_root = Path(raw.get("data_root", "data"))
    external_editor_raw = raw.get("external_editor", {}) or {}
    external_editor = ExternalEditorConfig(
        command=_coerce_str_list(external_editor_raw.get("command")),
    )

    return AppConfig(data_root=data_root, external_editor=external_editor)
