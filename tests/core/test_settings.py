from __future__ import annotations

import json
from pathlib import Path

import pytest

from sclm.core.errors import ConfigError
from sclm.core.models import ConflictPolicy, OrganizeMode
from sclm.core.settings import (
    SETTINGS_SCHEMA_VERSION,
    Settings,
    SettingsStore,
    load_settings,
    save_settings,
    validate_filename_template,
)


def test_defaults():
    s = Settings()
    assert s.library_dir == "~/Music/SoundCloud"
    assert s.library_path == Path.home() / "Music" / "SoundCloud"
    assert s.bitrate_kbps == 320 and s.concurrency == 2
    assert s.organize_mode is OrganizeMode.PLAYLIST
    assert s.conflict_policy is ConflictPolicy.SKIP
    assert s.filename_template == "{artist} - {title}"
    assert s.ffmpeg_path == "" and s.ffmpeg_path_or_none is None
    assert s.beets_enabled is False and s.only_new_tracks is True and s.embed_artwork is True
    assert s.cookies_path is None


@pytest.mark.parametrize("changes", [
    {"bitrate_kbps": 160}, {"bitrate_kbps": "320"}, {"bitrate_kbps": True},
    {"concurrency": 0}, {"concurrency": 5}, {"concurrency": 2.0},
    {"organize_mode": "year"}, {"conflict_policy": "overwrite"},
    {"library_dir": "  "}, {"beets_enabled": "yes"}, {"ffmpeg_path": None},
    {"filename_template": "{artist}"},
])
def test_invalid_values_raise_config_error(changes):
    with pytest.raises(ConfigError):
        Settings().replace(**changes)


@pytest.mark.parametrize("bitrate", [128, 192, 256, 320])
def test_allowed_bitrates(bitrate):
    assert Settings(bitrate_kbps=bitrate).bitrate_kbps == bitrate


def test_enum_fields_accept_strings():
    s = Settings(organize_mode="genre", conflict_policy="keep_both")
    assert s.organize_mode is OrganizeMode.GENRE
    assert s.conflict_policy is ConflictPolicy.KEEP_BOTH


@pytest.mark.parametrize("template", [
    "{artist} - {title}", "{index} {title}", "{title} [{track_id}]",
    "{year} - {album} - {artist} - {title} ({genre}) {playlist}",
])
def test_valid_templates(template):
    validate_filename_template(template)


@pytest.mark.parametrize("template,match", [
    ("", "empty"), ("{artist}", "title"), ("{foo} {title}", "Unknown field"),
    ("{} {title}", "named"), ("{0} {title}", "named"), ("{title:>10}", "Format"),
    ("{title!r}", "Format"), ("{artist}/{title}", "/"), ("{title}.mp3", "extension"),
    ("{title", "Invalid"),
])
def test_invalid_templates(template, match):
    with pytest.raises(ConfigError, match=match):
        validate_filename_template(template)


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "cfg" / "settings.json"
    s = Settings(library_dir="/music", bitrate_kbps=192, concurrency=3,
                 organize_mode=OrganizeMode.ARTIST, conflict_policy=ConflictPolicy.ASK,
                 beets_enabled=True, ffmpeg_path="/usr/bin/ffmpeg")
    save_settings(s, path)
    data = json.loads(path.read_text())
    assert data["schema_version"] == SETTINGS_SCHEMA_VERSION
    assert data["organize_mode"] == "artist"
    assert load_settings(path) == s
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]  # no temp leftovers


def test_load_missing_file_returns_defaults(tmp_path):
    path = tmp_path / "settings.json"
    assert load_settings(path) == Settings()
    assert not path.exists()


def test_load_is_lenient_about_bad_values_and_unknown_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1, "bitrate_kbps": 999, "concurrency": 3, "organize_mode": "nope",
        "filename_template": "{title} - {artist}", "unknown_key": 1,
    }))
    s = load_settings(path)
    assert s.bitrate_kbps == 320 and s.organize_mode is OrganizeMode.PLAYLIST
    assert s.concurrency == 3 and s.filename_template == "{title} - {artist}"


def test_load_corrupt_file_moves_it_aside(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert load_settings(path) == Settings()
    assert not path.exists()
    assert (tmp_path / "settings.json.bad").read_text() == "{not json"


def test_load_non_object_json_returns_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2]")
    assert load_settings(path) == Settings()


def test_settings_store_update_persists_and_notifies(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.get() == Settings()
    seen = []
    unsubscribe = store.subscribe(lambda old, new: seen.append((old.concurrency, new.concurrency)))
    store.subscribe(lambda old, new: 1 / 0)  # failing listener is swallowed

    store.update(store.get().replace(concurrency=4))
    assert store.get().concurrency == 4
    assert seen == [(2, 4)]
    assert load_settings(path).concurrency == 4
    assert SettingsStore(path).get().concurrency == 4

    unsubscribe()
    unsubscribe()  # idempotent
    store.update(store.get().replace(concurrency=1))
    assert seen == [(2, 4)]


def test_settings_store_initial_value_is_not_written(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path, initial=Settings(concurrency=1))
    assert store.get().concurrency == 1 and store.path == path and not path.exists()


def test_paths_expand_user():
    s = Settings(library_dir="~/x", cookies_file="~/c.txt")
    assert s.library_path == Path.home() / "x"
    assert s.cookies_path == Path.home() / "c.txt"
