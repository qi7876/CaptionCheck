from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QHeaderView,
    QSplitter,
    QSpinBox,
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


class MainWindow(QMainWindow):
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
        self._suppress_seek = False
        self._slider_dragging = False
        self._step_hold_left = False
        self._step_hold_right = False
        self._step_in_progress = False
        self._step_target_frame: int | None = None

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

        self._video_widget = QVideoWidget()
        self._video_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._video_widget.setMinimumSize(0, 0)
        self._player = QMediaPlayer(self)
        if hasattr(self._player, "setNotifyInterval"):
            self._player.setNotifyInterval(5)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)
        self._player.positionChanged.connect(self._on_position_changed)
        self._step_poll_timer = QTimer(self)
        self._step_poll_timer.setInterval(10)
        self._step_poll_timer.timeout.connect(self._on_step_tick)

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

        self._frame_jump = QSpinBox()
        self._frame_jump.setRange(0, 0)
        self._frame_jump.setSingleStep(1)
        self._frame_jump.setKeyboardTracking(False)
        self._frame_jump.setMinimumSize(0, 0)
        self._frame_jump.lineEdit().returnPressed.connect(self._jump_to_spinbox_frame)

        self._jump_button = QPushButton("Jump")
        self._jump_button.clicked.connect(self._jump_to_spinbox_frame)

        self._open_json_button = QPushButton("Open JSON")
        self._open_json_button.clicked.connect(self._open_current_json)

        controls = QHBoxLayout()
        controls.addWidget(self._play_button)
        controls.addWidget(QLabel("Speed:"))
        controls.addWidget(self._speed_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Frame:"))
        controls.addWidget(self._frame_jump)
        controls.addWidget(self._jump_button)
        controls.addStretch(1)
        controls.addWidget(self._frame_info)
        controls.addWidget(self._reviewed_checkbox)
        controls.addWidget(self._open_json_button)

        bottom_widget = QWidget()
        bottom_widget.setMinimumSize(0, 0)
        bottom_layout = QVBoxLayout()
        bottom_layout.addWidget(self._frame_slider)
        bottom_layout.addLayout(controls)
        bottom_widget.setLayout(bottom_layout)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setMinimumSize(0, 0)
        right_splitter.addWidget(self._video_widget)
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

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_Up), self, activated=lambda: self._step_speed(1))
        QShortcut(QKeySequence(Qt.Key.Key_Down), self, activated=lambda: self._step_speed(-1))

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        if self._items:
            self._select_first_item()

    def eventFilter(self, watched: object, event: object) -> bool:  # noqa: N802
        if isinstance(event, QEvent) and event.type() == QEvent.Type.MouseButtonPress:
            if self._frame_jump.hasFocus() or self._frame_jump.lineEdit().hasFocus():
                global_pos = None
                if hasattr(event, "globalPosition"):
                    global_pos = event.globalPosition().toPoint()
                elif hasattr(event, "globalPos"):
                    global_pos = event.globalPos()
                if global_pos is not None:
                    local_pos = self._frame_jump.mapFromGlobal(global_pos)
                    if not self._frame_jump.rect().contains(local_pos):
                        self._frame_jump.clearFocus()
        if isinstance(event, QKeyEvent):
            if not self.isActiveWindow():
                return super().eventFilter(watched, event)
            if self._frame_jump.hasFocus() or self._frame_jump.lineEdit().hasFocus():
                return super().eventFilter(watched, event)

            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Left:
                    if not event.isAutoRepeat():
                        self._step_hold_left = True
                        self._maybe_start_step()
                    return True
                if event.key() == Qt.Key.Key_Right:
                    if not event.isAutoRepeat():
                        self._step_hold_right = True
                        self._maybe_start_step()
                    return True

            if event.type() == QEvent.Type.KeyRelease:
                if event.key() == Qt.Key.Key_Left:
                    if not event.isAutoRepeat():
                        self._step_hold_left = False
                    return True
                if event.key() == Qt.Key.Key_Right:
                    if not event.isAutoRepeat():
                        self._step_hold_right = False
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
        self._video_widget.setFocus()

    def _load_item(self, item: DatasetItem) -> None:
        if self._current_item and self._current_item.dir_path.resolve() == item.dir_path.resolve():
            return

        self._player.pause()
        self._play_button.setText("Play")
        self._current_item = item

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

        self._frame_jump.blockSignals(True)
        if self._total_frames > 0:
            self._frame_jump.setRange(0, max(0, self._total_frames - 1))
            self._frame_jump.setValue(0)
        else:
            self._frame_jump.setRange(0, 0)
            self._frame_jump.setValue(0)
        self._frame_jump.blockSignals(False)

        self._suppress_seek = True
        self._frame_slider.setValue(0)
        self._suppress_seek = False

        self._update_frame_info(0)

        self._player.setSource(QUrl.fromLocalFile(str(item.video_path.resolve())))
        self._player.setPlaybackRate(float(self._speed_combo.currentData()))
        self._player.setPosition(0)

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self._play_button.setText("Play")
        else:
            self._player.play()
            self._play_button.setText("Pause")

    def _on_speed_changed(self) -> None:
        self._player.setPlaybackRate(float(self._speed_combo.currentData()))

    def _on_slider_pressed(self) -> None:
        self._slider_dragging = True

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        self._seek_to_frame(self._frame_slider.value())

    def _on_slider_moved(self, frame: int) -> None:
        self._update_frame_info(frame)
        self._seek_to_frame(frame)

    def _on_position_changed(self, position_ms: int) -> None:
        if self._slider_dragging:
            return
        self._update_position_ui(position_ms)

    def _update_position_ui(self, position_ms: int) -> int:
        frame = self._frame_from_position_ms(position_ms)
        if frame < 0:
            frame = 0
        if self._total_frames:
            frame = min(frame, self._total_frames - 1)
        self._suppress_seek = True
        self._frame_slider.setValue(frame)
        self._suppress_seek = False
        if not self._frame_jump.hasFocus():
            self._frame_jump.blockSignals(True)
            self._frame_jump.setValue(frame)
            self._frame_jump.blockSignals(False)
        self._update_frame_info(frame)
        return frame

    def _frame_from_position_ms(self, position_ms: int) -> int:
        if self._fps <= 0:
            return 0
        return int((position_ms * self._fps) / 1000.0 + 1e-6)

    def _position_ms_from_frame(self, frame: int) -> int:
        if self._fps <= 0:
            return 0
        return int(round((frame / self._fps) * 1000.0))

    def _seek_to_frame(self, frame: int) -> None:
        if self._suppress_seek or self._fps <= 0:
            return
        frame = max(0, frame)
        if self._total_frames:
            frame = min(frame, self._total_frames - 1)
        self._player.setPosition(self._position_ms_from_frame(frame))

    def _maybe_start_step(self) -> None:
        if self._step_in_progress or self._total_frames <= 0 or self._slider_dragging:
            return
        direction = 0
        if self._step_hold_right and not self._step_hold_left:
            direction = 1
        if self._step_hold_left and not self._step_hold_right:
            direction = -1
        if direction == 0:
            return

        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self._play_button.setText("Play")

        current_frame = self._frame_slider.value()
        target_frame = current_frame + direction
        if target_frame < 0 or target_frame >= self._total_frames:
            return
        self._start_step(direction, target_frame)

    def _start_step(self, direction: int, target_frame: int) -> None:
        self._step_in_progress = True
        self._step_target_frame = int(target_frame)
        step_rate = 2.0
        if direction < 0:
            self._player.setPosition(self._position_ms_from_frame(int(target_frame)))
        self._player.setPlaybackRate(step_rate)
        self._player.play()
        self._step_poll_timer.start()

    def _on_step_tick(self) -> None:
        if not self._step_in_progress or self._step_target_frame is None:
            self._step_poll_timer.stop()
            return
        current_frame = self._update_position_ui(self._player.position())
        if current_frame < self._step_target_frame:
            return

        self._step_poll_timer.stop()
        self._player.pause()
        self._play_button.setText("Play")
        self._player.setPlaybackRate(float(self._speed_combo.currentData()))
        self._step_in_progress = False
        self._step_target_frame = None
        QTimer.singleShot(0, self._maybe_start_step)

    def _update_frame_info(self, frame: int) -> None:
        if self._total_frames:
            self._frame_info.setText(f"Frame: {frame} / {self._total_frames - 1} (total {self._total_frames})")
        else:
            self._frame_info.setText("Frame: - / -")

    def _jump_to_spinbox_frame(self) -> None:
        self._frame_jump.interpretText()
        self._jump_to_frame(self._frame_jump.value())

    def _jump_to_frame(self, frame: int) -> None:
        if self._total_frames <= 0:
            return
        frame = max(0, min(int(frame), self._total_frames - 1))
        self._frame_slider.setValue(frame)
        self._seek_to_frame(frame)

    def _step_speed(self, delta: int) -> None:
        index = self._speed_combo.currentIndex()
        index = max(0, min(index + int(delta), self._speed_combo.count() - 1))
        self._speed_combo.setCurrentIndex(index)

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
