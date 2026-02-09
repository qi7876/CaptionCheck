from __future__ import annotations

import shutil
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QProcess, QTimer, Qt
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig
from ..dataset import DatasetItem, iter_dataset_items
from ..external_editor import open_path_in_editor
from ..json_io import read_json, write_json_atomic


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class MainWindow(QMainWindow):
    _PIXMAP_CACHE_SIZE = 128

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._items = iter_dataset_items(config.data_root)
        self._item_by_dir: dict[Path, DatasetItem] = {
            item.dir_path.resolve(): item for item in self._items
        }
        self._event_node_by_dir: dict[Path, QTreeWidgetItem] = {}
        self._review_indicator_by_dir: dict[Path, QCheckBox] = {}

        self._current_item: DatasetItem | None = None
        self._fps = 10.0
        self._total_frames = 0
        self._current_frame = 0
        self._suppress_seek = False
        self._slider_dragging = False

        self._playing = False
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(15)
        self._play_timer.timeout.connect(self._on_play_tick)
        self._play_last_time = 0.0
        self._play_frame_accum = 0.0

        self._step_hold_left = False
        self._step_hold_right = False
        self._step_timer = QTimer(self)
        self._step_timer.setInterval(15)
        self._step_timer.timeout.connect(self._on_step_hold_tick)
        self._step_last_time = 0.0
        self._step_frame_accum = 0.0

        self._frames_dir: Path | None = None
        self._pixmap_cache: OrderedDict[int, QPixmap] = OrderedDict()

        self._ffmpeg_path = shutil.which("ffmpeg")
        self._gen_process: QProcess | None = None
        self._gen_stdout_buffer = ""
        self._gen_tmp_dir: Path | None = None
        self._gen_final_dir: Path | None = None
        self._gen_expected_total_frames: int | None = None
        self._gen_expected_fps: float | None = None

        self.setWindowTitle("CaptionCheck")
        self.resize(1200, 800)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Sport/Event", "Reviewed"])
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 68)
        self._tree.setMinimumSize(0, 0)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self._populate_tree()

        self._frame_view = QLabel("Select a video")
        self._frame_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_view.setMinimumSize(0, 0)
        self._frame_view.setStyleSheet("background-color: black; color: white;")
        self._frame_view.setScaledContents(True)
        self._frame_view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

        self._play_button = QPushButton("Play")
        self._play_button.clicked.connect(self._toggle_play)

        self._speed_combo = QComboBox()
        for rate in [0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 8.0]:
            self._speed_combo.addItem(f"{rate:g}x", rate)
        self._speed_combo.setCurrentIndex(2)
        self._speed_combo.currentIndexChanged.connect(self._on_speed_changed)

        self._frame_slider = QSlider(Qt.Orientation.Horizontal)
        self._frame_slider.setSingleStep(1)
        self._frame_slider.setPageStep(10)
        self._frame_slider.sliderPressed.connect(self._on_slider_pressed)
        self._frame_slider.sliderReleased.connect(self._on_slider_released)
        self._frame_slider.sliderMoved.connect(self._on_slider_moved)

        self._frame_info = QLabel("Frame: - / -")

        self._reviewed_checkbox = QCheckBox("Reviewed")
        self._reviewed_checkbox.stateChanged.connect(self._on_reviewed_changed)

        self._open_json_button = QPushButton("Open JSON")
        self._open_json_button.clicked.connect(self._open_current_json)

        self._clear_frames_button = QPushButton("Clear Frames")
        self._clear_frames_button.clicked.connect(self._clear_frame_cache)

        controls = QHBoxLayout()
        controls.addWidget(self._play_button)
        controls.addWidget(QLabel("Speed:"))
        controls.addWidget(self._speed_combo)
        controls.addSpacing(12)
        controls.addStretch(1)
        controls.addWidget(self._frame_info)
        controls.addWidget(self._reviewed_checkbox)
        controls.addWidget(self._open_json_button)
        controls.addWidget(self._clear_frames_button)

        bottom_widget = QWidget()
        bottom_widget.setMinimumSize(0, 0)
        bottom_layout = QVBoxLayout()
        bottom_layout.addWidget(self._frame_slider)
        bottom_layout.addLayout(controls)
        bottom_widget.setLayout(bottom_layout)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setMinimumSize(0, 0)
        right_splitter.addWidget(self._frame_view)
        right_splitter.addWidget(bottom_widget)
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 0)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setMinimumSize(0, 0)
        main_splitter.addWidget(self._tree)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        self.setCentralWidget(main_splitter)

        self._status_text = QLabel("")
        self._status_progress = QProgressBar()
        self._status_progress.setVisible(False)
        self._status_progress.setMinimumWidth(220)
        self.statusBar().addWidget(self._status_text, 1)
        self.statusBar().addPermanentWidget(self._status_progress)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        if self._items:
            self._select_first_item()

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._cancel_frame_generation()
        self._set_playing(False)
        self._step_timer.stop()
        super().closeEvent(event)  # type: ignore[misc]

    def eventFilter(self, watched: object, event: object) -> bool:  # noqa: N802
        if isinstance(event, QKeyEvent):
            if not self.isActiveWindow():
                return super().eventFilter(watched, event)

            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Space:
                    if not event.isAutoRepeat():
                        self._toggle_play()
                    return True
                if event.key() == Qt.Key.Key_Up:
                    if not event.isAutoRepeat():
                        self._step_speed(1)
                    return True
                if event.key() == Qt.Key.Key_Down:
                    if not event.isAutoRepeat():
                        self._step_speed(-1)
                    return True
                if event.key() == Qt.Key.Key_Left:
                    if not event.isAutoRepeat():
                        self._step_hold_left = True
                        self._start_step_hold()
                    return True
                if event.key() == Qt.Key.Key_Right:
                    if not event.isAutoRepeat():
                        self._step_hold_right = True
                        self._start_step_hold()
                    return True

            if event.type() == QEvent.Type.KeyRelease:
                if event.key() == Qt.Key.Key_Left:
                    if not event.isAutoRepeat():
                        self._step_hold_left = False
                        self._maybe_stop_step_hold()
                    return True
                if event.key() == Qt.Key.Key_Right:
                    if not event.isAutoRepeat():
                        self._step_hold_right = False
                        self._maybe_stop_step_hold()
                    return True
        return False

    def _populate_tree(self) -> None:
        sport_nodes: dict[str, QTreeWidgetItem] = {}
        self._event_node_by_dir = {}
        self._review_indicator_by_dir = {}
        for item in self._items:
            sport_node = sport_nodes.get(item.sport)
            if sport_node is None:
                sport_node = QTreeWidgetItem([item.sport, ""])
                sport_nodes[item.sport] = sport_node
                self._tree.addTopLevelItem(sport_node)

            event_node = QTreeWidgetItem([item.event, ""])
            event_node.setData(0, Qt.ItemDataRole.UserRole, str(item.dir_path.resolve()))
            reviewed = False
            try:
                long_caption = read_json(item.long_caption_path)
                reviewed = bool(long_caption.get("reviewed", False))
            except Exception:
                reviewed = False
            reviewed_indicator = QCheckBox()
            reviewed_indicator.setChecked(reviewed)
            reviewed_indicator.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            reviewed_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            reviewed_indicator.setTristate(False)

            sport_node.addChild(event_node)

            indicator_container = QWidget()
            indicator_container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            indicator_container.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
            indicator_layout = QHBoxLayout(indicator_container)
            indicator_layout.setContentsMargins(0, 0, 0, 0)
            indicator_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            indicator_layout.addWidget(reviewed_indicator)
            self._tree.setItemWidget(event_node, 1, indicator_container)
            self._event_node_by_dir[item.dir_path.resolve()] = event_node
            self._review_indicator_by_dir[item.dir_path.resolve()] = reviewed_indicator

        for sport_node in sport_nodes.values():
            sport_node.setExpanded(True)

    def _select_first_item(self) -> None:
        top = self._tree.topLevelItem(0)
        if top is None or top.childCount() == 0:
            return
        self._tree.setCurrentItem(top.child(0))

    def _on_tree_selection_changed(self) -> None:
        node = self._tree.currentItem()
        if node is None or node.parent() is None:
            return
        dir_str = node.data(0, Qt.ItemDataRole.UserRole)
        if not dir_str:
            return
        item = self._item_by_dir.get(Path(str(dir_str)))
        if item is None:
            return
        self._load_item(item)

    def _load_item(self, item: DatasetItem) -> None:
        if self._current_item and self._current_item.dir_path.resolve() == item.dir_path.resolve():
            return

        self._set_playing(False)
        self._step_timer.stop()
        self._step_hold_left = False
        self._step_hold_right = False
        self._cancel_frame_generation()

        self._current_item = item
        self._pixmap_cache.clear()
        self._frames_dir = None
        self._current_frame = 0

        long_caption = read_json(item.long_caption_path)
        info = long_caption.get("info") or {}
        self._fps = float(info.get("fps") or 10.0)
        self._total_frames = int(info.get("total_frames") or 0)

        reviewed = bool(long_caption.get("reviewed", False))
        self._reviewed_checkbox.blockSignals(True)
        self._reviewed_checkbox.setChecked(reviewed)
        self._reviewed_checkbox.blockSignals(False)
        self._set_tree_reviewed_state(item.dir_path.resolve(), reviewed)

        if self._total_frames > 0:
            self._frame_slider.setRange(0, max(0, self._total_frames - 1))
        else:
            self._frame_slider.setRange(0, 0)

        self._suppress_seek = True
        self._frame_slider.setValue(0)
        self._suppress_seek = False
        self._update_frame_info(0)

        self._ensure_frames_for_current_item()

    def _toggle_play(self) -> None:
        if not self._frames_ready():
            return
        self._set_playing(not self._playing)

    def _set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return

        if playing:
            if not self._frames_ready():
                return
            self._step_timer.stop()
            self._play_button.setText("Pause")
            self._playing = True
            self._play_last_time = time.monotonic()
            self._play_frame_accum = 0.0
            self._play_timer.start()
            return

        self._play_timer.stop()
        self._playing = False
        self._play_button.setText("Play")

    def _on_speed_changed(self) -> None:
        if self._playing:
            # The timer tick reads the current combo value.
            pass

    def _on_slider_pressed(self) -> None:
        self._slider_dragging = True
        self._set_playing(False)
        self._step_timer.stop()

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        if not self._frames_ready():
            return
        self._set_current_frame(self._frame_slider.value())

    def _on_slider_moved(self, frame: int) -> None:
        if not self._frames_ready():
            return
        self._set_current_frame(frame)

    def _set_current_frame(self, frame: int) -> None:
        if not self._frames_ready():
            return
        frame = max(0, int(frame))
        if self._total_frames:
            frame = min(frame, self._total_frames - 1)
        if frame == self._current_frame:
            return
        self._current_frame = frame
        if not self._slider_dragging:
            self._suppress_seek = True
            self._frame_slider.setValue(frame)
            self._suppress_seek = False
        self._update_frame_info(frame)
        self._display_frame(frame)

    def _display_frame(self, frame: int) -> None:
        frames_dir = self._frames_dir
        if frames_dir is None:
            return
        pixmap = self._pixmap_cache.get(frame)
        if pixmap is not None and not pixmap.isNull():
            self._pixmap_cache.move_to_end(frame)
            self._frame_view.setText("")
            self._frame_view.setPixmap(pixmap)
            return

        frame_path = frames_dir / f"{frame:06d}.jpg"
        if not frame_path.exists():
            self._frame_view.setPixmap(QPixmap())
            self._frame_view.setText(f"Missing frame {frame}")
            return

        pixmap = QPixmap(str(frame_path))
        if pixmap.isNull():
            self._frame_view.setPixmap(QPixmap())
            self._frame_view.setText(f"Failed to load frame {frame}")
            return

        self._pixmap_cache[frame] = pixmap
        self._pixmap_cache.move_to_end(frame)
        while len(self._pixmap_cache) > self._PIXMAP_CACHE_SIZE:
            self._pixmap_cache.popitem(last=False)
        self._frame_view.setText("")
        self._frame_view.setPixmap(pixmap)

    def _on_play_tick(self) -> None:
        if not self._playing or not self._frames_ready() or self._fps <= 0:
            return

        now = time.monotonic()
        delta = now - self._play_last_time
        self._play_last_time = now

        rate = float(self._speed_combo.currentData() or 1.0)
        self._play_frame_accum += delta * self._fps * rate
        advance = int(self._play_frame_accum)
        if advance <= 0:
            return
        self._play_frame_accum -= advance

        target = self._current_frame + advance
        if self._total_frames and target >= self._total_frames:
            target = self._total_frames - 1
            self._set_current_frame(target)
            self._set_playing(False)
            return
        self._set_current_frame(target)

    def _step_speed(self, delta: int) -> None:
        index = self._speed_combo.currentIndex()
        index = max(0, min(index + int(delta), self._speed_combo.count() - 1))
        self._speed_combo.setCurrentIndex(index)

    def _step_direction(self) -> int:
        if self._step_hold_right and not self._step_hold_left:
            return 1
        if self._step_hold_left and not self._step_hold_right:
            return -1
        return 0

    def _start_step_hold(self) -> None:
        if not self._frames_ready() or self._fps <= 0 or self._slider_dragging:
            return
        direction = self._step_direction()
        if direction == 0:
            return
        self._set_playing(False)
        self._step_last_time = time.monotonic()
        self._step_frame_accum = 0.0
        self._step_timer.start()
        self._nudge_frame(direction)

    def _maybe_stop_step_hold(self) -> None:
        if self._step_direction() == 0:
            self._step_timer.stop()

    def _on_step_hold_tick(self) -> None:
        if not self._frames_ready() or self._fps <= 0 or self._slider_dragging:
            self._step_timer.stop()
            return
        direction = self._step_direction()
        if direction == 0:
            self._step_timer.stop()
            return

        now = time.monotonic()
        delta = now - self._step_last_time
        self._step_last_time = now

        step_rate = 1.0
        self._step_frame_accum += delta * self._fps * step_rate
        advance = int(self._step_frame_accum)
        if advance <= 0:
            return
        self._step_frame_accum -= advance

        self._nudge_frame(direction * advance)

    def _nudge_frame(self, delta: int) -> None:
        if not self._frames_ready() or self._total_frames <= 0:
            return
        target = self._current_frame + int(delta)
        target = max(0, min(target, self._total_frames - 1))
        self._set_current_frame(target)
        if target in {0, self._total_frames - 1}:
            self._maybe_stop_step_hold()

    def _update_frame_info(self, frame: int) -> None:
        if self._total_frames:
            self._frame_info.setText(
                f"Frame: {frame} / {self._total_frames - 1} (total {self._total_frames})"
            )
        else:
            self._frame_info.setText("Frame: - / -")

    def _frames_ready(self) -> bool:
        return self._frames_dir is not None and self._total_frames > 0

    def _frame_cache_root(self) -> Path:
        return self._config.data_root / "tmp" / "frames"

    def _frames_dir_for_item(self, item: DatasetItem) -> Path:
        return self._frame_cache_root() / item.sport / item.event

    def _frames_meta_path(self, frames_dir: Path) -> Path:
        return frames_dir / "meta.json"

    def _frames_cache_valid(self, frames_dir: Path, item: DatasetItem) -> bool:
        meta_path = self._frames_meta_path(frames_dir)
        if not meta_path.exists():
            return False
        try:
            meta = read_json(meta_path)
        except Exception:
            return False

        try:
            video_stat = item.video_path.stat()
        except OSError:
            return False

        if int(meta.get("video_mtime_ns") or 0) != int(video_stat.st_mtime_ns):
            return False
        if int(meta.get("video_size") or 0) != int(video_stat.st_size):
            return False

        fps = float(meta.get("fps") or 0.0)
        total_frames = int(meta.get("total_frames") or 0)
        if fps <= 0 or total_frames <= 0:
            return False
        if self._fps and abs(fps - self._fps) > 1e-3:
            return False
        if self._total_frames and total_frames != self._total_frames:
            return False

        first_frame = frames_dir / "000000.jpg"
        last_frame = frames_dir / f"{total_frames - 1:06d}.jpg"
        if not first_frame.exists() or not last_frame.exists():
            return False
        return True

    def _ensure_frames_for_current_item(self) -> None:
        if self._current_item is None:
            return

        if self._ffmpeg_path is None:
            self._set_generation_status("ffmpeg not found; cannot generate frames.", active=False)
            self._set_controls_enabled(False)
            self._frame_view.setPixmap(QPixmap())
            self._frame_view.setText("ffmpeg not found")
            return

        frames_dir = self._frames_dir_for_item(self._current_item)
        tmp_dir = frames_dir.with_name(frames_dir.name + ".inprogress")

        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if self._frames_cache_valid(frames_dir, self._current_item):
            self._frames_dir = frames_dir
            self._set_controls_enabled(True)
            self._set_generation_status("Frames ready (cached).", active=False)
            self._display_frame(0)
            return

        if frames_dir.exists():
            shutil.rmtree(frames_dir, ignore_errors=True)

        self._start_frame_generation(
            video_path=self._current_item.video_path,
            fps=self._fps,
            total_frames=self._total_frames,
            tmp_dir=tmp_dir,
            final_dir=frames_dir,
        )

    def _start_frame_generation(
        self,
        *,
        video_path: Path,
        fps: float,
        total_frames: int,
        tmp_dir: Path,
        final_dir: Path,
    ) -> None:
        self._cancel_frame_generation()
        self._set_playing(False)
        self._step_timer.stop()

        tmp_dir.parent.mkdir(parents=True, exist_ok=True)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        output_pattern = str(tmp_dir / "%06d.jpg")

        args = [
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(video_path.resolve()),
        ]
        if fps > 0:
            args.extend(["-vf", f"fps={fps:g}"])
        args.extend(["-start_number", "0"])
        if total_frames > 0:
            args.extend(["-frames:v", str(total_frames)])
        args.extend(["-q:v", "2"])
        args.extend(["-progress", "pipe:1", "-nostats"])
        args.append(output_pattern)

        proc = QProcess(self)
        proc.setProgram(self._ffmpeg_path or "ffmpeg")
        proc.setArguments(args)
        proc.readyReadStandardOutput.connect(self._on_ffmpeg_stdout)
        proc.finished.connect(self._on_ffmpeg_finished)
        proc.start()

        self._gen_process = proc
        self._gen_stdout_buffer = ""
        self._gen_tmp_dir = tmp_dir
        self._gen_final_dir = final_dir
        self._gen_expected_total_frames = int(total_frames) if total_frames > 0 else None
        self._gen_expected_fps = float(fps) if fps > 0 else None

        self._pixmap_cache.clear()
        self._frames_dir = None
        self._set_controls_enabled(False)
        self._frame_view.setPixmap(QPixmap())
        self._frame_view.setText("Generating frames…")

        if total_frames > 0:
            self._status_progress.setRange(0, total_frames)
            self._status_progress.setValue(0)
        else:
            self._status_progress.setRange(0, 0)
        self._set_generation_status("Generating frames…", active=True)

    def _on_ffmpeg_stdout(self) -> None:
        proc = self.sender()
        if proc is None or proc is not self._gen_process:
            return
        if self._gen_process is None:
            return
        text = bytes(self._gen_process.readAllStandardOutput()).decode("utf-8", errors="ignore")
        if not text:
            return
        self._gen_stdout_buffer += text
        while "\n" in self._gen_stdout_buffer:
            line, rest = self._gen_stdout_buffer.split("\n", 1)
            self._gen_stdout_buffer = rest
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "frame":
                try:
                    frame = int(value)
                except ValueError:
                    continue
                self._update_generation_progress(frame)

    def _update_generation_progress(self, frame: int) -> None:
        if self._gen_expected_total_frames:
            self._status_progress.setValue(min(frame, self._gen_expected_total_frames))
            self._status_text.setText(f"Generating frames… {frame}/{self._gen_expected_total_frames}")
        else:
            self._status_text.setText(f"Generating frames… {frame}")

    def _on_ffmpeg_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        proc = self.sender()
        if proc is None or proc is not self._gen_process:
            return

        tmp_dir = self._gen_tmp_dir
        final_dir = self._gen_final_dir
        expected_total = self._gen_expected_total_frames
        expected_fps = self._gen_expected_fps

        self._gen_process = None
        self._gen_tmp_dir = None
        self._gen_final_dir = None
        self._gen_expected_total_frames = None
        self._gen_expected_fps = None
        self._gen_stdout_buffer = ""

        if exit_status != QProcess.ExitStatus.NormalExit or exit_code != 0:
            if tmp_dir and tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._set_generation_status("Frame generation failed.", active=False)
            self._frame_view.setText("Frame generation failed")
            return

        if tmp_dir is None or final_dir is None:
            self._set_generation_status("Frame generation finished (missing dirs).", active=False)
            return

        if expected_total:
            last = tmp_dir / f"{expected_total - 1:06d}.jpg"
            if not last.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
                self._set_generation_status("Frame generation incomplete; regenerating needed.", active=False)
                self._frame_view.setText("Frame generation incomplete")
                return
            total_frames = expected_total
        else:
            frames = list(tmp_dir.glob("*.jpg"))
            total_frames = len(frames)
            if total_frames <= 0:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                self._set_generation_status("No frames generated.", active=False)
                self._frame_view.setText("No frames generated")
                return

        self._status_progress.setRange(0, total_frames)
        self._status_progress.setValue(total_frames)

        try:
            video_stat = (self._current_item.video_path if self._current_item else None).stat()  # type: ignore[union-attr]
        except Exception:
            video_stat = None

        meta: dict[str, Any] = {
            "generated_at": _utc_now_iso(),
            "fps": float(expected_fps or self._fps),
            "total_frames": int(total_frames),
        }
        if self._current_item is not None:
            meta["video_path"] = str(self._current_item.video_path.resolve())
        if video_stat is not None:
            meta["video_mtime_ns"] = int(video_stat.st_mtime_ns)
            meta["video_size"] = int(video_stat.st_size)

        try:
            write_json_atomic(tmp_dir / "meta.json", meta)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._set_generation_status("Failed to write meta.json.", active=False)
            self._frame_view.setText("Failed to write meta.json")
            return

        if final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp_dir.replace(final_dir)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._set_generation_status("Failed to finalize frame cache.", active=False)
            QMessageBox.critical(self, "Cache failed", str(e))
            return

        self._frames_dir = final_dir
        self._total_frames = total_frames
        if self._total_frames > 0:
            self._frame_slider.setRange(0, max(0, self._total_frames - 1))
        self._set_controls_enabled(True)
        self._set_generation_status("Frames ready.", active=False)
        self._current_frame = 0
        self._suppress_seek = True
        self._frame_slider.setValue(0)
        self._suppress_seek = False
        self._update_frame_info(0)
        self._display_frame(0)

    def _cancel_frame_generation(self) -> None:
        if self._gen_process is None:
            return
        proc = self._gen_process
        tmp_dir = self._gen_tmp_dir

        try:
            proc.kill()
            proc.waitForFinished(1000)
        except Exception:
            pass

        self._gen_process = None
        self._gen_tmp_dir = None
        self._gen_final_dir = None
        self._gen_expected_total_frames = None
        self._gen_expected_fps = None
        self._gen_stdout_buffer = ""

        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._set_generation_status("", active=False)

    def _set_generation_status(self, message: str, *, active: bool) -> None:
        self._status_text.setText(message)
        self._status_progress.setVisible(active)
        if not active:
            self._status_progress.setRange(0, 100)
            self._status_progress.setValue(0)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._play_button.setEnabled(enabled)
        self._speed_combo.setEnabled(enabled)
        self._frame_slider.setEnabled(enabled)

    def _clear_frame_cache(self) -> None:
        self._cancel_frame_generation()
        self._set_playing(False)
        cache_root = self._frame_cache_root()
        if cache_root.exists():
            try:
                shutil.rmtree(cache_root)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "Clear failed", str(e))
                return
        self._pixmap_cache.clear()
        self._frames_dir = None
        self._frame_view.setPixmap(QPixmap())
        self._frame_view.setText("Frames cleared")
        self._set_generation_status("Frame cache cleared.", active=False)
        if self._current_item is not None:
            self._ensure_frames_for_current_item()

    def _set_tree_reviewed_state(self, dir_path: Path, reviewed: bool) -> None:
        indicator = self._review_indicator_by_dir.get(dir_path.resolve())
        if indicator is None:
            return
        indicator.setChecked(bool(reviewed))

    def _on_reviewed_changed(self, state: int) -> None:
        if self._current_item is None:
            return
        reviewed = Qt.CheckState(state) == Qt.CheckState.Checked
        try:
            long_caption = read_json(self._current_item.long_caption_path)
            long_caption["reviewed"] = bool(reviewed)
            write_json_atomic(self._current_item.long_caption_path, long_caption)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self._set_tree_reviewed_state(self._current_item.dir_path.resolve(), bool(reviewed))

    def _open_current_json(self) -> None:
        if self._current_item is None:
            return
        try:
            open_path_in_editor(self._current_item.long_caption_path, self._config.external_editor)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(e))
