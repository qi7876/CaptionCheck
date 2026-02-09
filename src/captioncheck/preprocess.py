from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dataset import DatasetItem, iter_dataset_items
from .json_io import read_json, write_json_atomic


PREPROCESS_VERSION = 1


@dataclass(frozen=True)
class PreprocessResult:
    item: DatasetItem
    status: str  # "skipped" | "processed" | "error"
    message: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def preprocess_item(item: DatasetItem) -> PreprocessResult:
    if item.preprocess_status_path.exists():
        return PreprocessResult(item=item, status="skipped", message="already preprocessed")

    try:
        long_caption: dict[str, Any] = read_json(item.long_caption_path)
        info = long_caption.get("info") or {}
        original_starting_frame = int(info.get("original_starting_frame") or 0)
        total_frames = int(info.get("total_frames") or 0)
        spans: list[dict[str, Any]] = list(long_caption.get("spans") or [])

        changed = False

        if "reviewed" not in long_caption:
            long_caption["reviewed"] = False
            changed = True

        needs_shift = False
        if spans and original_starting_frame and total_frames:
            max_end = max(int(s.get("end_frame", 0)) for s in spans)
            min_start = min(int(s.get("start_frame", 0)) for s in spans)
            if max_end > total_frames + 2 and min_start >= original_starting_frame - 2:
                needs_shift = True

        if needs_shift:
            for span in spans:
                start = int(span.get("start_frame", 0)) - original_starting_frame
                end = int(span.get("end_frame", 0)) - original_starting_frame
                if start < 0:
                    start = 0
                if end < 0:
                    end = 0
                span["start_frame"] = start
                span["end_frame"] = end
            long_caption["spans"] = spans
            changed = True

        if changed:
            write_json_atomic(item.long_caption_path, long_caption)

        status_payload = {
            "preprocess_version": PREPROCESS_VERSION,
            "preprocessed_at": _utc_now_iso(),
            "original_starting_frame": original_starting_frame,
            "total_frames": total_frames,
            "shifted_spans": bool(needs_shift),
        }
        write_json_atomic(item.preprocess_status_path, status_payload)
        return PreprocessResult(item=item, status="processed", message="ok")
    except Exception as e:  # noqa: BLE001
        return PreprocessResult(item=item, status="error", message=str(e))


def preprocess_dataset(data_root: Path) -> list[PreprocessResult]:
    results: list[PreprocessResult] = []
    for item in iter_dataset_items(data_root):
        results.append(preprocess_item(item))
    return results

