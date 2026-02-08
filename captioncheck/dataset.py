from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatasetItem:
    sport: str
    event: str
    dir_path: Path
    video_path: Path
    long_caption_path: Path
    run_meta_path: Path
    preprocess_status_path: Path


def iter_dataset_items(data_root: Path) -> list[DatasetItem]:
    items: list[DatasetItem] = []
    if not data_root.exists():
        return items

    for sport_dir in sorted([p for p in data_root.iterdir() if p.is_dir()], key=lambda p: p.name):
        for event_dir in sorted([p for p in sport_dir.iterdir() if p.is_dir()], key=lambda p: p.name):
            video_path = event_dir / "segment.mp4"
            long_caption_path = event_dir / "long_caption.json"
            run_meta_path = event_dir / "run_meta.json"
            preprocess_status_path = event_dir / "preprocess_status.json"

            if video_path.exists() and long_caption_path.exists() and run_meta_path.exists():
                items.append(
                    DatasetItem(
                        sport=sport_dir.name,
                        event=event_dir.name,
                        dir_path=event_dir,
                        video_path=video_path,
                        long_caption_path=long_caption_path,
                        run_meta_path=run_meta_path,
                        preprocess_status_path=preprocess_status_path,
                    )
                )
    return items

