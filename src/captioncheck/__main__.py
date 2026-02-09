from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .preprocess import preprocess_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="captioncheck")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to captioncheck_config.json (default: ./captioncheck_config.json)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)

    try:
        from PySide6.QtWidgets import QApplication  # noqa: WPS433

        from .gui.main_window import MainWindow  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        print("PySide6 is required to run the GUI.", file=sys.stderr)
        print(f"Import error: {e}", file=sys.stderr)
        return 1

    results = preprocess_dataset(config.data_root)
    errors = [r for r in results if r.status == "error"]
    if errors:
        print("Preprocess errors:", file=sys.stderr)
        for r in errors:
            print(f"- {r.item.dir_path}: {r.message}", file=sys.stderr)

    app = QApplication(sys.argv)
    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

