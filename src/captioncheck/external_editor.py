from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import ExternalEditorConfig


def open_path_in_editor(path: Path, editor: ExternalEditorConfig) -> None:
    path = path.resolve()

    if editor.command:
        subprocess.Popen([*editor.command, str(path)])  # noqa: S603
        return

    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603
        return

    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606
        return

    subprocess.Popen(["xdg-open", str(path)])  # noqa: S603

