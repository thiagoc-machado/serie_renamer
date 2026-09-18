from pathlib import Path

import pytest

import app.services as services
from app.services import (
    EpisodeCandidate,
    EpisodeGroup,
    RenameInstruction,
    apply_changes,
    delete_empty_folders,
    delete_media_items,
    apply_online_titles_to_groups,
    build_target_filename,
    filter_instructions_by_group,
    parse_form_instructions,
    fetch_episode_titles,
    _pick_tmdb_show,
    _query_variants,
    scan_media,
    safe_move,
    scan_library,
)


def test_build_target_filename():
    filename = build_target_filename("My Series", 1, 2, "Pilot", ".mp4")
    assert filename == "My Series - S01E02 - Pilot.mp4"


def test_safe_move_disallows_existing_target(tmp_path: Path):
    source = tmp_path / "source.mp4"
    target = tmp_path / "target.mp4"
    source.write_text("dummy", encoding="utf-8")
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        safe_move(source, target)

    assert source.exists()
    assert target.exists()


def test_apply_changes_skips_existing_destination(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_text("dummy", encoding="utf-8")

    existing_dir = tmp_path / "My Series" / "Season 01"
    existing_dir.mkdir(parents=True)
    existing_target = existing_dir / "My Series - S01E01.mp4"
    existing_target.write_text("existing", encoding="utf-8")

    instructions = [
        RenameInstruction(
            source_path=source,
            source_root=tmp_path,
            destination_root=tmp_path,
            series_code="MY",
            series_name="My Series",
            season=1,
            episode=1,
            episode_title="",
        )
    ]

    result = apply_changes(instructions)

    assert result["updated"] == []
    assert any("Arquivo de destino já existe" in error for error in result["errors"])
    assert source.exists()
    assert existing_target.exists()


def test_apply_changes_moves_avi_without_mp4_metadata_processing(tmp_path: Path):
    source = tmp_path / "raw.avi"
    source.write_text("dummy", encoding="utf-8")
    instructions = [
        RenameInstruction(
            source_path=source,
            source_root=tmp_path,
            destination_root=tmp_path,
            series_code="SHOW",
            series_name="Show",
            season=1,
            episode=1,
            episode_title="Pilot",
        )
    ]

    result = apply_changes(instructions)

    assert result["errors"] == []
    assert not source.exists()
    assert (tmp_path / "Show" / "Season 01" / "Show - S01E01 - Pilot.avi").exists()


def test_parse_form_instructions_filters_outside_source_root(tmp_path: Path):
    source_root = tmp_path / "root"
    source_root.mkdir()
    outside = tmp_path / "outside" / "source.mp4"
    outside.parent.mkdir(parents=True)
    outside.write_text("dummy", encoding="utf-8")

    instructions = parse_form_instructions(
        source_root=source_root,
        destination_root=source_root,
        file_path=[str(outside)],
        series_code=["ABC"],
        series_name=["Test"],
        season=["1"],
        episode=["1"],
        episode_title=[""]
    )

    assert instructions == []


def test_parse_form_instructions_honors_selected_paths(tmp_path: Path):
    source_root = tmp_path / "root"
    source_root.mkdir()
    first = source_root / "a.mp4"
    second = source_root / "b.mp4"
    first.write_text("dummy", encoding="utf-8")
    second.write_text("dummy", encoding="utf-8")

    instructions = parse_form_instructions(
        source_root=source_root,
        destination_root=source_root,
        file_path=[str(first), str(second)],
        selected_paths={str(first.resolve())},
        series_code=["A", "B"],
        series_name=["Test", "Test"],
        season=["1", "1"],
        episode=["1", "2"],
        episode_title=["", ""],
    )

    assert [item.source_path.name for item in instructions] == ["a.mp4"]


def test_scan_media_ignores_generic_series_codes(tmp_path: Path):
    ignored = tmp_path / "Season.S01E01.mp4"
    kept = tmp_path / "GOOD.S01E01.mp4"
    ignored.write_text("dummy", encoding="utf-8")
    kept.write_text("dummy", encoding="utf-8")

    groups = scan_media(tmp_path)

    assert [group.series_code for group in groups] == ["GOOD"]


def test_apply_online_titles_to_groups_fills_missing_titles(monkeypatch):
    group = EpisodeGroup(
        series_code="GOOD",
        guessed_name="Good Series",
        count=1,
        seasons=[1],
        folder_hints=[],
        entries=[
            EpisodeCandidate(
                source_path=Path("/tmp/GOOD.S01E01.mp4"),
                relative_dir=".",
                filename="GOOD.S01E01.mp4",
                extension=".mp4",
                series_code="GOOD",
                season=1,
                episode=1,
                guessed_series_name="Good Series",
                suggested_episode_title="",
                folder_hint="",
                pattern_name="test",
            )
        ],
    )

    monkeypatch.setattr(
        "app.services.fetch_episode_titles",
        lambda series_name, seasons, language=None: {(1, 1): "Pilot"} if series_name == "Good Series" else {},
    )

    result = apply_online_titles_to_groups([group])

    assert result["applied"] == 1
    assert group.entries[0].suggested_episode_title == "Pilot"


def test_filter_instructions_by_group_keeps_only_target_series():
    instructions = [
        RenameInstruction(
            source_path=Path("/tmp/a.mp4"),
            source_root=Path("/tmp"),
            destination_root=Path("/tmp"),
            series_code="AAA",
            series_name="Alpha",
            season=1,
            episode=1,
            episode_title="",
        ),
        RenameInstruction(
            source_path=Path("/tmp/b.mp4"),
            source_root=Path("/tmp"),
            destination_root=Path("/tmp"),
            series_code="BBB",
            series_name="Beta",
            season=1,
            episode=1,
            episode_title="",
        ),
    ]

    filtered = filter_instructions_by_group(instructions, "bbb")

    assert [item.series_code for item in filtered] == ["BBB"]


def test_query_variants_generate_better_search_forms():
    variants = _query_variants("Ataque de titans")

    assert "Ataque de titans" in variants
    assert "ataque titans" in variants
    assert "titans" in variants


def test_pick_tmdb_show_prefers_closer_match():
    results = [
        {"id": 1, "name": "Some Other Show", "original_name": "Some Other Show"},
        {"id": 2, "name": "Attack on Titan", "original_name": "Shingeki no Kyojin"},
    ]

    chosen = _pick_tmdb_show(results, "Ataque de titans")

    assert chosen["id"] == 2


def test_fetch_episode_titles_uses_tmdb_season_payload(monkeypatch):
    calls = []

    def fake_search_show(series_name, language=None):
        calls.append(("search", series_name, language))
        return {"id": 77}

    def fake_tmdb_get_json(path, params=None):
        calls.append((path, params))
        if path == "/tv/77/season/1":
            return {
                "episodes": [
                    {"episode_number": 1, "name": "Pilot"},
                    {"episode_number": 2, "name": "Second"},
                ]
            }
        return {}

    monkeypatch.setattr("app.services._query_variants", lambda series_name: [series_name])
    monkeypatch.setattr("app.services._tmdb_search_show", fake_search_show)
    monkeypatch.setattr("app.services._tmdb_get_json", fake_tmdb_get_json)

    titles = fetch_episode_titles("My Show", {1})

    assert titles == {(1, 1): "Pilot", (1, 2): "Second"}
    assert calls[0][0] == "search"
    assert any(call[0] == "/tv/77/season/1" for call in calls)


def test_fetch_episode_titles_falls_back_to_tvmaze(monkeypatch):
    def fake_search_show(series_name, language=None):
        return None

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout=8):
        url = request.full_url
        if "/search/shows" in url:
            return FakeResponse([
                {"score": 100, "show": {"id": 9, "name": "Fallback Show"}},
            ])
        if "/shows/9/episodes" in url:
            return FakeResponse([
                {"season": 1, "number": 1, "name": "TVMaze Pilot"},
            ])
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("app.services._query_variants", lambda series_name: [series_name])
    monkeypatch.setattr("app.services._tmdb_search_show", fake_search_show)
    monkeypatch.setattr("app.services.urlopen", fake_urlopen)

    titles = fetch_episode_titles("Fallback Show", {1})

    assert titles == {(1, 1): "TVMaze Pilot"}


def test_scan_library_groups_by_folder_and_flags_metadata_mismatch(tmp_path: Path, monkeypatch):
    root = tmp_path / "media"
    show_a = root / "Show A"
    show_b = root / "Show B"
    show_a.mkdir(parents=True)
    show_b.mkdir(parents=True)
    file_a = show_a / "SHOWA.S01E01.mp4"
    file_b = show_b / "SHOWB.S01E01.mp4"
    file_a.write_text("dummy", encoding="utf-8")
    file_b.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(
        "app.services.read_mp4_metadata",
        lambda path: {"tvsh": ["Wrong Show"]} if path == file_b else {"tvsh": ["Show A"]},
    )

    result = scan_library(root)

    assert [group.folder_name for group in result["series_groups"]] == ["Show A", "Show B"]
    assert len(result["metadata_mismatches"]) == 1
    assert result["metadata_mismatches"][0].filename == "SHOWB.S01E01.mp4"


def test_scan_library_keeps_organized_files_and_groups_all_seasons(tmp_path: Path, monkeypatch):
    root = tmp_path / "media"
    season_one = root / "A Show" / "Season 01"
    season_two = root / "A Show" / "Season 02"
    season_one.mkdir(parents=True)
    season_two.mkdir(parents=True)
    first = season_one / "A Show - S01E01 - Pilot.mp4"
    second = season_two / "A Show - S02E03.mp4"
    first.write_text("dummy", encoding="utf-8")
    second.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(
        "app.services.read_mp4_metadata",
        lambda path: {"\xa9nam": ["Metadata Pilot"]} if path == first else {},
    )

    result = scan_library(root)

    assert len(result["series_groups"]) == 1
    group = result["series_groups"][0]
    assert group.folder_path == "A Show"
    assert group.seasons == [1, 2]
    assert [entry.filename for entry in group.entries] == [first.name, second.name]
    assert all(entry.organized for entry in group.entries)
    assert group.entries[0].suggested_episode_title == "Metadata Pilot"
    assert group.entries[1].suggested_episode_title == ""


def test_scan_library_filter_matches_episode_metadata_title(tmp_path: Path, monkeypatch):
    root = tmp_path / "media" / "A Show"
    root.mkdir(parents=True)
    pilot = root / "A Show - S01E01.mp4"
    finale = root / "A Show - S01E02.mp4"
    pilot.write_text("dummy", encoding="utf-8")
    finale.write_text("dummy", encoding="utf-8")

    def metadata(path: Path):
        title = "Pilot" if path == pilot else "Finale"
        return {"tvsh": ["A Show"], "\xa9nam": [title]}

    monkeypatch.setattr("app.services.read_mp4_metadata", metadata)

    result = scan_library(root.parent, filter_text="Pilot")

    assert len(result["series_groups"]) == 1
    assert [entry.filename for entry in result["series_groups"][0].entries] == [pilot.name]


def test_scan_library_supports_common_homeserver_video_extensions(tmp_path: Path, monkeypatch):
    root = tmp_path / "media" / "A Show" / "Season 01"
    root.mkdir(parents=True)
    episode = root / "A Show - S01E01.mkv"
    episode.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr("app.services.read_mp4_metadata", lambda path: {})

    result = scan_library(root.parents[1])

    assert result["series_groups"][0].entries[0].filename == episode.name


def test_delete_empty_folders_removes_only_empty_dirs(tmp_path: Path):
    root = tmp_path / "root"
    data_root = tmp_path / "data"
    empty = root / "empty"
    non_empty = root / "non-empty"
    empty.mkdir(parents=True)
    non_empty.mkdir(parents=True)
    (non_empty / "file.txt").write_text("dummy", encoding="utf-8")

    services.DATA_ROOT = data_root
    services.HISTORY_DIR = data_root / "history"
    services.ALIASES_FILE = data_root / "series_aliases.json"
    services.ALLOWED_ROOTS = [root.resolve()]

    result = delete_empty_folders([empty, non_empty])

    assert empty.exists() is False
    assert non_empty.exists() is True
    assert any("Pasta vazia removida" in line for line in result["updated"])


def test_delete_media_items_requires_safe_root_and_only_empty_folders(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    media = root / "episode.mp4"
    empty = root / "empty"
    non_empty = root / "non-empty"
    media.write_text("dummy", encoding="utf-8")
    empty.mkdir()
    non_empty.mkdir()
    (non_empty / "keep.txt").write_text("keep", encoding="utf-8")
    services.ALLOWED_ROOTS = [root.resolve()]

    result = delete_media_items([media, empty, non_empty])

    assert not media.exists()
    assert not empty.exists()
    assert non_empty.exists()
    assert any("não está vazia" in error for error in result["errors"])
