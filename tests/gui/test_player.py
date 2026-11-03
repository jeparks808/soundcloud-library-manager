"""Local player: queue/transport logic on a fake backend, real QtMultimedia decoding of
generated MP3s (muted), and the Library -> player wiring."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QSettings, QUrl, Signal
from PySide6.QtMultimedia import QMediaDevices, QMediaMetaData, QMediaPlayer

from sclm.core.models import LibraryFile, LibrarySource
from sclm.gui.player import PlayerBar, PlayerController

S = QMediaPlayer.MediaStatus


class FakeMediaPlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)
    metaDataChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sources: list[str] = []
        self.pos = 0
        self.calls: list[str] = []

    def setSource(self, url: QUrl) -> None:
        self.sources.append(url.toLocalFile())

    def play(self) -> None:
        self.calls.append("play")
        self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)

    def pause(self) -> None:
        self.calls.append("pause")
        self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PausedState)

    def stop(self) -> None:
        self.calls.append("stop")
        self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)

    def position(self) -> int:
        return self.pos

    def setPosition(self, ms: int) -> None:
        self.pos = ms
        self.calls.append(f"seek:{ms}")

    def metaData(self) -> QMediaMetaData:
        return QMediaMetaData()


class FakeAudio(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.volume = None
        self.muted = None

    def setVolume(self, v: float) -> None:
        self.volume = v

    def setMuted(self, m: bool) -> None:
        self.muted = m


def files(tmp_path: Path, n: int) -> list[LibraryFile]:
    return [LibraryFile(path=tmp_path / f"t{i}.mp3", source=LibrarySource.DOWNLOAD,
                        artist=f"Artist {i}", title=f"Title {i}", duration_s=60)
            for i in range(n)]


@pytest.fixture
def ini(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "player.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def fake(ini):
    holder = {}

    def factory(parent):
        holder["player"] = FakeMediaPlayer(parent)
        holder["audio"] = FakeAudio(parent)
        return holder["player"], holder["audio"]

    controller = PlayerController(backend_factory=factory, settings=ini)
    return controller, holder


def test_lazy_backend_and_queue_navigation(fake, tmp_path):
    c, h = fake
    assert "player" not in h                     # no multimedia plugin at startup
    tracks = []
    c.track_changed.connect(tracks.append)
    c.play_files(files(tmp_path, 3), start=1)
    p = h["player"]
    assert p.sources == [str(tmp_path / "t1.mp3")] and c.playing
    c.next()
    assert c.index == 2
    c.next()                                     # last track: Next does nothing
    assert c.index == 2
    p.pos = 10_000
    c.previous()                                 # > 3 s in: restart the track
    assert c.index == 2 and p.calls[-1] == "seek:0"
    p.pos = 500
    c.previous()
    assert c.index == 1
    c.toggle()
    assert not c.playing and p.calls[-1] == "pause"
    c.toggle()
    assert c.playing
    assert [t.title for t in tracks] == ["Title 1", "Title 2", "Title 1"]


def test_end_of_media_advances_then_stops(fake, tmp_path):
    c, h = fake
    c.play_files(files(tmp_path, 2))
    p = h["player"]
    p.mediaStatusChanged.emit(S.EndOfMedia)
    assert c.index == 1 and len(p.sources) == 2
    p.mediaStatusChanged.emit(S.EndOfMedia)
    assert c.index == 1 and not c.playing and p.calls[-1] == "seek:0"


def test_missing_files_are_reported_and_skipped(fake, tmp_path, qtbot):
    c, h = fake
    messages = []
    c.message.connect(messages.append)
    c.play_files(files(tmp_path, 3))
    p = h["player"]
    p.errorOccurred.emit(QMediaPlayer.Error.ResourceError, "No such file")
    p.mediaStatusChanged.emit(S.InvalidMedia)    # same failure, reported once
    qtbot.waitUntil(lambda: c.index == 1)
    assert messages == ["Can't play t0.mp3: file missing or unreadable."]
    p.mediaStatusChanged.emit(S.InvalidMedia)
    qtbot.waitUntil(lambda: c.index == 2)
    p.mediaStatusChanged.emit(S.InvalidMedia)    # every file failed: stop, no loop
    qtbot.wait(20)
    assert c.index == 2 and not c.playing and len(messages) == 3


def test_enqueue_remove_clear(fake, tmp_path):
    c, _h = fake
    a, b, d = files(tmp_path, 3)
    assert c.enqueue([a]) == 1 and c.current == a     # empty queue: starts playing
    c.enqueue([b, d])
    assert c.queue == [a, b, d] and c.index == 0
    c.remove(1)
    assert c.queue == [a, d] and c.index == 0
    c.remove(0)                                        # removing the current plays the next
    assert c.current == d
    c.clear()
    assert c.queue == [] and c.current is None and not c.playing


def test_volume_and_mute_persist(fake, ini, tmp_path):
    c, h = fake
    c.play_files(files(tmp_path, 1))
    c.set_volume(40)
    c.set_muted(True)
    assert h["audio"].muted is True and 0 < h["audio"].volume < 0.4   # logarithmic scale
    again = PlayerController(backend_factory=lambda parent: (FakeMediaPlayer(parent),
                                                             FakeAudio(parent)), settings=ini)
    assert again.volume == 40 and again.muted
    c.set_volume(55)                                    # moving the slider unmutes
    assert not c.muted and h["audio"].muted is False


def test_player_bar_reflects_controller(fake, tmp_path, qtbot):
    c, h = fake
    bar = PlayerBar(c)
    qtbot.addWidget(bar)
    assert bar.title.text() == "Nothing playing" and not bar.play_btn.isEnabled()
    c.play_files(files(tmp_path, 2))
    assert bar.title.text() == "Title 0" and bar.artist.text() == "Artist 0"
    assert bar.play_btn.accessibleName() == "Pause" and bar.next_btn.isEnabled()
    assert bar.total.text() == "1:00" and bar.queue_btn.text() == "Queue (2)"
    h["player"].positionChanged.emit(61_000)
    assert bar.elapsed.text() == "1:01"
    bar.seek.setValue(30_000)
    bar._seek_released()
    assert h["player"].calls[-1] == "seek:30000"
    bar.volume.setValue(20)
    assert c.volume == 20
    bar.mute_btn.click()
    assert c.muted and bar.mute_btn.accessibleName() == "Unmute"
    bar.show_queue()
    assert bar._popup.list.count() == 2 and bar._popup.list.item(0).text().startswith("▶")
    bar._popup.hide()


# --- real QtMultimedia (FFmpeg backend), muted ------------------------------------------------

@pytest.fixture
def audio_device(qapp) -> None:  # noqa: ANN001
    if not QMediaDevices.audioOutputs():
        pytest.skip("no audio output device")


def test_real_mp3_playback_advances_and_skips_missing(qtbot, ini, tmp_path, _tiny_mp3_master,
                                                     audio_device):
    one = tmp_path / "one.mp3"
    two = tmp_path / "two.mp3"
    shutil.copyfile(_tiny_mp3_master, one)
    shutil.copyfile(_tiny_mp3_master, two)
    lib = [LibraryFile(path=p, source=LibrarySource.SCAN, title=p.stem)
           for p in (one, tmp_path / "missing.mp3", two)]
    c = PlayerController(settings=ini)
    c.set_muted(True)
    messages, positions = [], []
    c.message.connect(messages.append)
    c.position_changed.connect(positions.append)
    c.play_files(lib)
    qtbot.waitUntil(lambda: c.playing, timeout=5000)
    qtbot.waitUntil(lambda: c.index == 2, timeout=10000)      # one.mp3 ended, missing skipped
    assert any(p > 200 for p in positions)
    assert len(messages) == 1 and "missing.mp3" in messages[0]
    qtbot.waitUntil(lambda: not c.playing, timeout=10000)    # end of the queue
    c.shutdown()


def test_real_mp3_seek_and_artwork(qtbot, ini, tmp_path, ffmpeg_exe, audio_device):
    import subprocess

    cover = tmp_path / "cover.png"
    subprocess.run([str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=orange:s=64x64", "-frames:v", "1", str(cover)], check=True)
    song = tmp_path / "song.mp3"
    subprocess.run([str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=330:duration=6", "-i", str(cover), "-map", "0:a",
                    "-map", "1:v", "-c:a", "libmp3lame", "-b:a", "96k", "-c:v", "png",
                    "-id3v2_version", "3", "-disposition:v", "attached_pic", str(song)],
                   check=True, timeout=60)
    c = PlayerController(settings=ini)
    c.set_muted(True)
    art, durations = [], []
    c.artwork_changed.connect(art.append)
    c.duration_changed.connect(durations.append)
    c.play_files([LibraryFile(path=song, source=LibrarySource.SCAN)])
    qtbot.waitUntil(lambda: c.playing and any(d > 5000 for d in durations), timeout=5000)
    c.seek(4000)
    qtbot.waitUntil(lambda: c.position() >= 4000, timeout=5000)
    qtbot.waitUntil(lambda: any(a is not None for a in art), timeout=5000)
    assert next(a for a in art if a is not None).width() == 64
    c.pause()
    qtbot.waitUntil(lambda: not c.playing, timeout=5000)
    c.shutdown()


def test_library_double_click_plays_page_as_queue(qtbot, ctx, tmp_path):
    from sclm.gui.main_window import MainWindow, PAGE_LIBRARY

    for i in range(3):
        ctx.storage.add_library_file(LibraryFile(
            path=tmp_path / f"song{i}.mp3", source=LibrarySource.SCAN, artist="A",
            title=f"Song {i}"))
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    played = []
    window.player.play_files = lambda queue, start: played.append(
        ([f.title for f in queue], start))
    window.show_page(PAGE_LIBRARY)
    view = window.library_view
    view.table.selectRow(1)
    index = view.model.index(1, 0)
    view.table.doubleClicked.emit(index)
    assert played == [(["Song 0", "Song 1", "Song 2"], 1)]
    queued = []
    window.player.enqueue = lambda fs: queued.append([f.title for f in fs]) or len(fs)
    view.enqueue_btn.click()
    assert queued == [["Song 1"]]
    window.close()
