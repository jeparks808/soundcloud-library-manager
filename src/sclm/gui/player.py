"""Local playback of library files: QtMultimedia (the FFmpeg backend shipped with PySide6).

``PlayerController`` owns the play queue and one ``QMediaPlayer`` (created on first use, so
startup doesn't load the multimedia plugin). Decoding and file reads happen in Qt's media
threads; nothing here touches the disk on the GUI thread. Missing or unreadable files are
reported and skipped. ``PlayerBar`` is the persistent control strip under every page.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, QPoint, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sclm.core.models import LibraryFile
from sclm.gui.common import format_duration, set_role

log = logging.getLogger(__name__)

RESTART_THRESHOLD_MS = 3000     # "Previous" restarts the track after this much playback
DEFAULT_VOLUME = 80
ART_SIZE = 48


def track_label(f: LibraryFile) -> str:
    title = f.title or f.path.stem
    return f"{f.artist} – {title}" if f.artist else title


def _default_backend(parent: QObject) -> tuple[QObject, QObject]:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    player = QMediaPlayer(parent)
    audio = QAudioOutput(parent)
    player.setAudioOutput(audio)
    return player, audio


class PlayerController(QObject):
    """Play queue + transport. All slots run on the GUI thread."""

    track_changed = Signal(object)      # LibraryFile | None
    playing_changed = Signal(bool)
    position_changed = Signal(int)      # ms
    duration_changed = Signal(int)      # ms
    queue_changed = Signal()
    artwork_changed = Signal(object)    # QImage | None
    volume_changed = Signal(int, bool)  # 0..100, muted
    message = Signal(str)

    def __init__(self, parent: QObject | None = None, *,
                 backend_factory: Callable[[QObject], tuple[QObject, QObject]] | None = None,
                 settings: QSettings | None = None) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory or _default_backend
        self._settings = settings
        self._player = None
        self._audio = None
        self._queue: list[LibraryFile] = []
        self._index = -1
        self._failures = 0
        self._failed_index: tuple[int, str] | None = None
        self._playing = False
        s = self._qsettings()
        self._volume = max(0, min(100, int(s.value("player/volume", DEFAULT_VOLUME))))
        self._muted = str(s.value("player/muted", "false")).lower() == "true"

    def _qsettings(self) -> QSettings:
        return self._settings if self._settings is not None else QSettings()

    # --- state ------------------------------------------------------------------------------

    @property
    def queue(self) -> list[LibraryFile]:
        return list(self._queue)

    @property
    def index(self) -> int:
        return self._index

    @property
    def current(self) -> LibraryFile | None:
        return self._queue[self._index] if 0 <= self._index < len(self._queue) else None

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    def position(self) -> int:
        return int(self._player.position()) if self._player is not None else 0

    # --- backend ----------------------------------------------------------------------------

    def _ensure_backend(self) -> None:
        if self._player is not None:
            return
        from PySide6.QtMultimedia import QMediaPlayer

        self._player, self._audio = self._backend_factory(self)
        p = self._player
        p.positionChanged.connect(lambda ms: self.position_changed.emit(int(ms)))
        p.durationChanged.connect(lambda ms: self.duration_changed.emit(int(ms)))
        p.playbackStateChanged.connect(
            lambda state: self._set_playing(state == QMediaPlayer.PlaybackState.PlayingState))
        p.mediaStatusChanged.connect(self._on_status)
        p.errorOccurred.connect(self._on_error)
        p.metaDataChanged.connect(self._on_metadata)
        self._apply_volume()

    def _apply_volume(self) -> None:
        if self._audio is None:
            return
        from PySide6.QtMultimedia import QtAudio

        linear = QtAudio.convertVolume(self._volume / 100.0,
                                       QtAudio.VolumeScale.LogarithmicVolumeScale,
                                       QtAudio.VolumeScale.LinearVolumeScale)
        self._audio.setVolume(linear)
        self._audio.setMuted(self._muted)

    def _set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.playing_changed.emit(playing)

    def _on_status(self, status: object) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        S = QMediaPlayer.MediaStatus
        if status == S.EndOfMedia:
            self._failures = 0
            self._advance(stop_at_end=True)
        elif status == S.InvalidMedia:
            self._skip_failed("unsupported or unreadable")
        elif status in (S.LoadedMedia, S.BufferedMedia):
            self._failures = 0

    def _on_error(self, error: object, _text: str = "") -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if error == QMediaPlayer.Error.NoError:
            return
        reason = ("file missing or unreadable" if error == QMediaPlayer.Error.ResourceError
                  else "unsupported format" if error == QMediaPlayer.Error.FormatError
                  else "playback error")
        self._skip_failed(reason)

    def _skip_failed(self, reason: str) -> None:
        f = self.current
        if f is None or self._failed_index == (self._index, str(f.path)):
            return          # errorOccurred and InvalidMedia both fire for one failure
        self._failed_index = (self._index, str(f.path))
        self._failures += 1
        self.message.emit(f"Can't play {f.path.name}: {reason}.")
        log.info("Local playback failed for %s: %s", f.path.name, reason)
        if self._failures >= len(self._queue):
            self.stop()
            return
        QTimer.singleShot(0, lambda: self._advance(stop_at_end=True))

    def _on_metadata(self) -> None:
        from PySide6.QtMultimedia import QMediaMetaData

        md = self._player.metaData()
        image = md.value(QMediaMetaData.Key.CoverArtImage)   # ID3 APIC / attached picture
        self.artwork_changed.emit(image if isinstance(image, QImage) and not image.isNull()
                                  else None)

    # --- queue ------------------------------------------------------------------------------

    def play_files(self, files: Iterable[LibraryFile], start: int = 0) -> None:
        """Replace the queue with ``files`` and play ``files[start]``."""
        self._queue = list(files)
        self.queue_changed.emit()
        if self._queue:
            self.play_index(max(0, min(start, len(self._queue) - 1)))
        else:
            self.stop()

    def enqueue(self, files: Iterable[LibraryFile]) -> int:
        added = list(files)
        self._queue.extend(added)
        self.queue_changed.emit()
        if added and self.current is None:
            self.play_index(len(self._queue) - len(added))
        return len(added)

    def remove(self, i: int) -> None:
        if not 0 <= i < len(self._queue):
            return
        del self._queue[i]
        if i < self._index:
            self._index -= 1
        elif i == self._index:
            if self._index < len(self._queue):
                self.play_index(self._index)
            else:
                self.stop()
                self._index = -1
                self.track_changed.emit(None)
        self.queue_changed.emit()

    def clear(self) -> None:
        self.stop()
        self._queue = []
        self._index = -1
        self.track_changed.emit(None)
        self.queue_changed.emit()

    # --- transport --------------------------------------------------------------------------

    def play_index(self, i: int) -> None:
        if not 0 <= i < len(self._queue):
            return
        self._ensure_backend()
        self._index = i
        self._failed_index = None
        f = self._queue[i]
        self.artwork_changed.emit(None)
        self.track_changed.emit(f)
        self.queue_changed.emit()
        self._player.setSource(QUrl.fromLocalFile(str(f.path)))
        self._player.play()

    def play(self) -> None:
        if self.current is None:
            if self._queue:
                self.play_index(0)
            return
        self._ensure_backend()
        self._player.play()

    def pause(self) -> None:
        if self._player is not None and self._playing:
            self._player.pause()

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
        self._set_playing(False)

    def next(self) -> None:
        self._advance(stop_at_end=False)

    def _advance(self, *, stop_at_end: bool) -> None:
        if self._index + 1 < len(self._queue):
            self.play_index(self._index + 1)
        elif stop_at_end:
            self.stop()
            if self._player is not None:
                self._player.setPosition(0)

    def previous(self) -> None:
        if self._player is not None and self._player.position() > RESTART_THRESHOLD_MS:
            self._player.setPosition(0)
        elif self._index > 0:
            self.play_index(self._index - 1)
        elif self._player is not None:
            self._player.setPosition(0)

    def seek(self, ms: int) -> None:
        if self._player is not None:
            self._player.setPosition(max(0, int(ms)))

    def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, int(volume)))
        if self._volume and self._muted:
            self._muted = False
        self._apply_volume()
        self._save_volume()
        self.volume_changed.emit(self._volume, self._muted)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._apply_volume()
        self._save_volume()
        self.volume_changed.emit(self._volume, self._muted)

    def _save_volume(self) -> None:
        try:
            s = self._qsettings()
            s.setValue("player/volume", self._volume)
            s.setValue("player/muted", "true" if self._muted else "false")
        except Exception:
            log.debug("Could not save the player volume", exc_info=True)

    def shutdown(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())


class SeekSlider(QSlider):
    """Horizontal slider that jumps to the clicked position (not page steps)."""

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                   int(event.position().x()), self.width())
            self.setValue(value)
            self.sliderMoved.emit(value)
        super().mousePressEvent(event)


class QueuePopup(QFrame):
    """The play queue, shown above the Queue button. Double-click plays an entry."""

    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("queuePopup")
        self._controller = controller
        self.resize(460, 340)
        self.list = QListWidget()
        self.list.setAccessibleName("Play queue")
        self.list.itemDoubleClicked.connect(
            lambda item: controller.play_index(self.list.row(item)))
        remove = QPushButton("Remove")
        remove.setAccessibleName("Remove the selected track from the play queue")
        remove.clicked.connect(lambda: controller.remove(self.list.currentRow()))
        clear = QPushButton("Clear queue")
        clear.clicked.connect(controller.clear)
        buttons = QHBoxLayout()
        buttons.addWidget(set_role(QLabel("Double-click a track to play it"), "muted"), 1)
        buttons.addWidget(remove)
        buttons.addWidget(clear)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        controller.queue_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for i, f in enumerate(self._controller.queue):
            item = QListWidgetItem(("▶  " if i == self._controller.index else "    ")
                                   + track_label(f))
            item.setToolTip(str(f.path))
            self.list.addItem(item)
        if 0 <= self._controller.index < self.list.count():
            self.list.setCurrentRow(self._controller.index)


class PlayerBar(QFrame):
    """Persistent local-player strip: artwork, title, transport, seek, volume, queue."""

    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.setAccessibleName("Local player")
        self.controller = controller
        self._seeking = False
        self._popup: QueuePopup | None = None
        style = self.style()

        self.art = QLabel()
        self.art.setObjectName("playerArt")
        self.art.setFixedSize(ART_SIZE, ART_SIZE)
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setAccessibleName("Album artwork")
        self.title = QLabel("Nothing playing")
        self.title.setObjectName("playerTitle")
        self.title.setAccessibleName("Now playing title")
        self.artist = set_role(QLabel("Double-click a track in Library to play it"), "muted")
        self.artist.setAccessibleName("Now playing artist")
        for label in (self.title, self.artist):
            label.setMinimumWidth(80)
            label.setMaximumWidth(320)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.artist)
        text.addStretch(1)

        def tool(icon: QStyle.StandardPixmap, name: str, slot: Callable[[], None]) -> QToolButton:
            b = QToolButton()
            b.setIcon(style.standardIcon(icon))
            b.setIconSize(QSize(20, 20))
            b.setToolTip(name)
            b.setAccessibleName(name)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            return b

        SP = QStyle.StandardPixmap
        self.prev_btn = tool(SP.SP_MediaSkipBackward, "Previous track", controller.previous)
        self.play_btn = tool(SP.SP_MediaPlay, "Play", controller.toggle)
        self.play_btn.setIconSize(QSize(26, 26))
        self.stop_btn = tool(SP.SP_MediaStop, "Stop", controller.stop)
        self.next_btn = tool(SP.SP_MediaSkipForward, "Next track", controller.next)
        self.mute_btn = tool(SP.SP_MediaVolume, "Mute", lambda: controller.set_muted(
            not controller.muted))

        self.elapsed = set_role(QLabel("0:00"), "muted")
        self.total = set_role(QLabel("0:00"), "muted")
        for label in (self.elapsed, self.total):
            label.setMinimumWidth(42)
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.seek = SeekSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Playback position")
        self.seek.setRange(0, 0)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderMoved.connect(lambda v: self.elapsed.setText(format_duration(v / 1000)))
        self.seek.sliderReleased.connect(self._seek_released)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setAccessibleName("Volume")
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(100)
        self.volume.setValue(controller.volume)
        self.volume.valueChanged.connect(controller.set_volume)

        self.queue_btn = QPushButton("Queue")
        self.queue_btn.setAccessibleName("Show the play queue")
        self.queue_btn.clicked.connect(self.show_queue)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(6)
        row.addWidget(self.art)
        row.addLayout(text, 2)
        row.addSpacing(8)
        for b in (self.prev_btn, self.play_btn, self.stop_btn, self.next_btn):
            row.addWidget(b)
        row.addSpacing(8)
        row.addWidget(self.elapsed)
        row.addWidget(self.seek, 5)
        row.addWidget(self.total)
        row.addSpacing(8)
        row.addWidget(self.mute_btn)
        row.addWidget(self.volume)
        row.addWidget(self.queue_btn)

        controller.track_changed.connect(self._track_changed)
        controller.playing_changed.connect(self._playing_changed)
        controller.position_changed.connect(self._position_changed)
        controller.duration_changed.connect(self._duration_changed)
        controller.artwork_changed.connect(self._artwork_changed)
        controller.volume_changed.connect(self._volume_changed)
        controller.queue_changed.connect(self._update_buttons)
        self._volume_changed(controller.volume, controller.muted)
        self._artwork_changed(None)
        self._update_buttons()

    def install_shortcuts(self, window: QWidget) -> None:
        """Media keys and Ctrl+Space work anywhere in the main window."""
        c = self.controller
        for keys, slot in ((QKeySequence("Ctrl+Space"), c.toggle),
                           (QKeySequence(Qt.Key.Key_MediaTogglePlayPause), c.toggle),
                           (QKeySequence(Qt.Key.Key_MediaPlay), c.toggle),
                           (QKeySequence(Qt.Key.Key_MediaPause), c.pause),
                           (QKeySequence(Qt.Key.Key_MediaStop), c.stop),
                           (QKeySequence(Qt.Key.Key_MediaNext), c.next),
                           (QKeySequence(Qt.Key.Key_MediaPrevious), c.previous)):
            shortcut = QShortcut(keys, window)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)

    # --- controller -> widgets ---------------------------------------------------------------

    def _track_changed(self, f: LibraryFile | None) -> None:
        if f is None:
            self.title.setText("Nothing playing")
            self.artist.setText("Double-click a track in Library to play it")
            self.title.setToolTip("")
            self._duration_changed(0)
            self._position_changed(0)
        else:
            self.title.setText(f.title or f.path.stem)
            self.artist.setText(f.artist or "Unknown artist")
            self.title.setToolTip(str(f.path))
            if f.duration_s:
                self._duration_changed(int(f.duration_s * 1000))
        self._update_buttons()

    def _playing_changed(self, playing: bool) -> None:
        SP = QStyle.StandardPixmap
        self.play_btn.setIcon(self.style().standardIcon(
            SP.SP_MediaPause if playing else SP.SP_MediaPlay))
        name = "Pause" if playing else "Play"
        self.play_btn.setToolTip(name)
        self.play_btn.setAccessibleName(name)

    def _position_changed(self, ms: int) -> None:
        if not self._seeking:
            self.seek.setValue(ms)
            self.elapsed.setText(format_duration(ms / 1000))

    def _duration_changed(self, ms: int) -> None:
        if ms > 0 or self.controller.current is None:
            self.seek.setRange(0, max(0, ms))
            self.total.setText(format_duration(ms / 1000))

    def _seek_released(self) -> None:
        self._seeking = False
        self.controller.seek(self.seek.value())

    def _artwork_changed(self, image: QImage | None) -> None:
        if image is None:
            self.art.setPixmap(QPixmap())
            self.art.setText("♪")
            return
        self.art.setText("")
        self.art.setPixmap(QPixmap.fromImage(image).scaled(
            ART_SIZE, ART_SIZE, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation))

    def _volume_changed(self, volume: int, muted: bool) -> None:
        if self.volume.value() != volume:
            self.volume.blockSignals(True)
            self.volume.setValue(volume)
            self.volume.blockSignals(False)
        SP = QStyle.StandardPixmap
        self.mute_btn.setIcon(self.style().standardIcon(
            SP.SP_MediaVolumeMuted if muted else SP.SP_MediaVolume))
        name = "Unmute" if muted else "Mute"
        self.mute_btn.setToolTip(name)
        self.mute_btn.setAccessibleName(name)

    def _update_buttons(self, *_args: object) -> None:
        c = self.controller
        has_queue = bool(c.queue)
        self.play_btn.setEnabled(has_queue)
        self.stop_btn.setEnabled(c.current is not None)
        self.prev_btn.setEnabled(c.current is not None)
        self.next_btn.setEnabled(c.index + 1 < len(c.queue))
        self.seek.setEnabled(c.current is not None)
        self.queue_btn.setText(f"Queue ({len(c.queue)})" if has_queue else "Queue")

    def show_queue(self) -> None:
        if self._popup is None:
            self._popup = QueuePopup(self.controller, self)
        self._popup.refresh()
        anchor = self.queue_btn.mapToGlobal(QPoint(self.queue_btn.width(), 0))
        self._popup.move(anchor.x() - self._popup.width(), anchor.y() - self._popup.height() - 4)
        self._popup.show()
