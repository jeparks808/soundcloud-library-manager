from __future__ import annotations

import threading

from sclm.core.models import ConflictPolicy, OrganizeMode
from sclm.gui.settings_dialog import SettingsDialog


def fake_render(template, track, *, playlist_title=None):
    return template.replace("{artist}", track.artist).replace("{title}", track.title)


def make_dialog(qtbot, ctx, **kw):
    seen = {}

    def probe(configured):
        seen["thread"] = threading.current_thread().name
        seen["configured"] = configured
        return ("ffmpeg 7.0 (bundled)\n/x/ffmpeg", "2026.01.01")

    dialog = SettingsDialog(ctx, render_fn=kw.get("render_fn", fake_render), probe_fn=probe)
    qtbot.addWidget(dialog)
    return dialog, seen


def test_form_reflects_current_settings_and_shows_tools(qtbot, ctx):
    dialog, seen = make_dialog(qtbot, ctx)
    s = ctx.settings.get()
    assert dialog.library_edit.text() == s.library_dir
    assert dialog.bitrate_combo.currentData() == 320
    assert dialog.concurrency_spin.value() == 2
    assert dialog.template_preview.text() == "M83 - Midnight City.mp3"
    qtbot.waitUntil(lambda: "ffmpeg 7.0" in dialog.ffmpeg_info.text(), timeout=3000)
    assert dialog.ytdlp_info.text() == "2026.01.01"
    assert seen["thread"] != threading.main_thread().name
    assert dialog.is_valid and dialog.save_btn.isEnabled()


def test_config_error_is_shown_inline_and_blocks_save(qtbot, ctx):
    dialog, _ = make_dialog(qtbot, ctx)
    dialog.show()
    dialog.template_edit.setText("{artist}")                 # missing {title}
    assert not dialog.is_valid
    assert dialog.error_banner.isVisible()
    assert "{title}" in dialog.error_banner.message
    assert not dialog.save_btn.isEnabled()
    assert dialog.try_save() is False
    assert ctx.settings.get().filename_template == "{artist} - {title}"   # untouched

    dialog.template_edit.setText("{artist}/{title}")
    assert "'/'" in dialog.error_banner.message

    dialog.template_edit.setText("{nope} {title}")
    assert "Unknown field" in dialog.error_banner.message

    dialog.template_edit.setText("{title}")                  # fixed again
    assert dialog.is_valid and not dialog.error_banner.isVisible()
    assert dialog.save_btn.isEnabled()


def test_empty_library_folder_is_rejected(qtbot, ctx):
    dialog, _ = make_dialog(qtbot, ctx)
    dialog.show()
    dialog.library_edit.setText("   ")
    assert not dialog.is_valid
    assert "library folder" in dialog.error_banner.message.lower()


def test_save_updates_the_settings_store(qtbot, ctx):
    dialog, _ = make_dialog(qtbot, ctx)
    seen = []
    ctx.settings.subscribe(lambda old, new: seen.append((old, new)))
    dialog.bitrate_combo.setCurrentIndex(dialog.bitrate_combo.findData(192))
    dialog.concurrency_spin.setValue(3)
    dialog.organize_combo.setCurrentIndex(dialog.organize_combo.findData(OrganizeMode.GENRE))
    dialog.conflict_combo.setCurrentIndex(
        dialog.conflict_combo.findData(ConflictPolicy.KEEP_BOTH))
    dialog.only_new_check.setChecked(False)
    dialog.template_edit.setText("{index} {artist} - {title}")
    assert dialog.try_save() is True
    new = ctx.settings.get()
    assert (new.bitrate_kbps, new.concurrency) == (192, 3)
    assert new.organize_mode == OrganizeMode.GENRE
    assert new.conflict_policy == ConflictPolicy.KEEP_BOTH
    assert new.only_new_tracks is False
    assert new.filename_template == "{index} {artist} - {title}"
    assert len(seen) == 1
    assert ctx.paths.settings_file.exists()                  # persisted


def test_preview_not_implemented_is_graceful(qtbot, ctx):
    def stub(*a, **kw):
        raise NotImplementedError

    dialog, _ = make_dialog(qtbot, ctx, render_fn=stub)
    assert "not available" in dialog.template_preview.text()
    assert dialog.is_valid


def test_stale_probe_results_are_ignored(qtbot, ctx):
    dialog, _ = make_dialog(qtbot, ctx)
    qtbot.waitUntil(lambda: "ffmpeg 7.0" in dialog.ffmpeg_info.text(), timeout=3000)
    dialog._probe_seq += 5
    dialog._on_probe(1, ("old", "old"))
    assert dialog.ffmpeg_info.text() != "old"
