from __future__ import annotations

import json
import os
import re
import shutil
import uuid
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from mutagen.mp4 import MP4, MP4StreamInfoError
except ModuleNotFoundError:  # pragma: no cover
    MP4 = None

    class MP4StreamInfoError(Exception):
        pass

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov"}
RAW_PATTERNS = [
    re.compile(
        r"^(?P<code>[A-Z0-9]+?)[\s._-]*T(?P<season>\d{2})[\s._-]*EP(?P<episode>\d{2,3})$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<code>[A-Z0-9]+?)[\s._-]*S(?P<season>\d{2})[\s._-]*E(?P<episode>\d{2,3})$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<code>[A-Z0-9]+?)[\s._-]*(?P<season>\d{1,2})x(?P<episode>\d{2,3})$",
        re.IGNORECASE,
    ),
]
ORGANIZED_PATTERNS = [
    re.compile(r".+\s-\sS\d{2}E\d{2,3}(\s-\s.+)?$", re.IGNORECASE),
]
ORGANIZED_EPISODE_PATTERN = re.compile(
    r"^(?P<name>.+?)\s+-\s+S(?P<season>\d{1,2})E(?P<episode>\d{1,3})(?:\s+-\s+.+)?$",
    re.IGNORECASE,
)
SAFE_CHARS = re.compile(r"[^A-Za-z0-9À-ÿ _.\-]+")
MULTI_SPACE = re.compile(r"\s+")
GENERIC_FOLDER_NAMES = {"tv", "shows", "series", "temporadas", "videos", "media"}
GENERIC_LABELS = {
    "episode",
    "episodes",
    "season",
    "seasons",
    "serie",
    "series",
    "show",
    "shows",
    "tv",
    "temporada",
    "temporadas",
    "video",
    "videos",
    "media",
}

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
DATA_ROOT = Path(os.getenv("APP_DATA_DIR", str(DEFAULT_DATA_ROOT))).resolve()
ALIASES_FILE = DATA_ROOT / "series_aliases.json"
HISTORY_DIR = DATA_ROOT / "history"

NO_DELETE_MODE = True
ALLOWED_ROOTS: list[Path] = []

SONARR_URL = os.getenv("SONARR_URL", "").strip()
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "").strip()
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").strip()
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "").strip()
JELLYFIN_LIBRARY_ID = os.getenv("JELLYFIN_LIBRARY_ID", "").strip()
TMDB_BEARER_TOKEN = os.getenv("TMDB_BEARER_TOKEN", "").strip()
TMDB_DEFAULT_LANGUAGE = os.getenv("TMDB_DEFAULT_LANGUAGE", "pt-BR").strip() or "pt-BR"
TMDB_SEARCH_LANGUAGES = [TMDB_DEFAULT_LANGUAGE, "en-US", "es-ES", None]
SEARCH_STOPWORDS = {
    "a",
    "as",
    "de",
    "del",
    "da",
    "das",
    "do",
    "dos",
    "e",
    "em",
    "la",
    "las",
    "le",
    "los",
    "o",
    "os",
    "the",
    "and",
    "of",
    "on",
}


def allowed_roots_message() -> str:
    if not ALLOWED_ROOTS:
        return "raízes permitidas não configuradas"
    return ", ".join(str(root) for root in ALLOWED_ROOTS)


def outside_allowed_roots_message(path: Path) -> str:
    return f"{path}: fora das raízes permitidas. Use apenas caminhos dentro de: {allowed_roots_message()}."


@dataclass(slots=True)
class EpisodeCandidate:
    source_path: Path
    relative_dir: str
    filename: str
    extension: str
    series_code: str
    season: int
    episode: int
    guessed_series_name: str
    suggested_episode_title: str
    folder_hint: str
    pattern_name: str


@dataclass(slots=True)
class EpisodeGroup:
    series_code: str
    guessed_name: str
    count: int
    seasons: list[int]
    folder_hints: list[str]
    entries: list[EpisodeCandidate]


@dataclass(slots=True)
class FolderFileEntry:
    source_path: Path
    relative_dir: str
    filename: str
    extension: str
    series_folder: str
    season: int
    episode: int
    organized: bool
    pattern_name: str
    metadata_series_name: str
    metadata_matches_folder: bool


@dataclass(slots=True)
class FolderGroup:
    folder_path: str
    folder_name: str
    display_name: str
    anchor_id: str
    count: int
    seasons: list[int]
    entries: list[FolderFileEntry]


@dataclass(slots=True)
class PatternGroup:
    folder_path: str
    series_code: str
    guessed_name: str
    anchor_id: str
    count: int
    seasons: list[int]
    entries: list[EpisodeCandidate]


@dataclass(slots=True)
class EmptyFolderEntry:
    path: str
    relative_path: str
    folder_name: str
    anchor_id: str
    depth: int


@dataclass(slots=True)
class DirectoryTreeNode:
    path: str
    name: str
    anchor_id: str
    file_count: int
    series_count: int
    empty_count: int
    children: list["DirectoryTreeNode"]
    series_groups: list[Any]


@dataclass(slots=True)
class MetadataMismatchEntry:
    source_path: Path
    relative_dir: str
    filename: str
    folder_series_name: str
    metadata_series_name: str
    season: int
    episode: int
    pattern_name: str
    anchor_id: str


@dataclass(slots=True)
class RenameInstruction:
    source_path: Path
    source_root: Path
    destination_root: Path
    series_code: str
    series_name: str
    season: int
    episode: int
    episode_title: str = ""


def ensure_state_dirs() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def safe_move(source: Path, target: Path) -> None:
    """Move file only if target does not already exist.

    This prevents destructive overwrite or deletion of existing files.
    """
    resolved_source = source.resolve()
    resolved_target = target.resolve()
    if resolved_source == resolved_target:
        return
    if resolved_target.exists():
        raise FileExistsError(f"Destino existe e não será sobrescrito: {resolved_target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def sanitize_name(value: str) -> str:
    cleaned = SAFE_CHARS.sub("", value.strip())
    cleaned = MULTI_SPACE.sub(" ", cleaned)
    return cleaned.strip(" ._-")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", normalized)
    return MULTI_SPACE.sub(" ", normalized).strip().casefold()


def titleize_code(value: str) -> str:
    clean = sanitize_name(value.replace("_", " ").replace("-", " "))
    return clean.title() if clean else value


def tmdb_language_code(language: str | None) -> str | None:
    clean = (language or "").strip().lower()
    if not clean or clean == "original":
        return None
    if clean.startswith("pt"):
        return "pt-BR"
    if clean.startswith("es"):
        return "es-ES"
    if clean.startswith("en"):
        return "en-US"
    return language.strip()


def looks_generic_label(value: str) -> bool:
    clean = sanitize_name(value).lower()
    if not clean:
        return True
    if clean in GENERIC_LABELS:
        return True
    if clean.startswith("season "):
        return True
    if clean.startswith("temporada "):
        return True
    return False


def is_season_folder_name(value: str) -> bool:
    clean = sanitize_name(value).lower()
    if not clean:
        return False
    return bool(
        re.match(r"^(season|temporada|saison|stagione|temporada)\s*\d+$", clean)
        or re.match(r"^s\d{1,2}$", clean)
    )


def extract_series_folder(path: Path, root: Path) -> tuple[str, str]:
    relative_parent = path.parent.relative_to(root)
    parts = [part for part in relative_parent.parts if part not in (".", "")]
    folder_candidates: list[str] = []
    for part in parts:
        clean = sanitize_name(part)
        if not clean:
            continue
        if is_season_folder_name(clean):
            break
        if clean.lower() not in GENERIC_FOLDER_NAMES and not looks_generic_label(clean):
            folder_candidates.append(clean)
    if folder_candidates:
        series_folder = folder_candidates[0]
    elif parts:
        series_folder = sanitize_name(parts[0]) or sanitize_name(path.parent.name)
    else:
        series_folder = sanitize_name(path.parent.name) or sanitize_name(root.name) or path.stem

    # A series is the folder before the first Season/Sxx directory. This keeps
    # all seasons in one editable batch instead of creating one group per season.
    season_index = next(
        (index for index, part in enumerate(parts) if is_season_folder_name(part)),
        len(parts),
    )
    folder_path = str(Path(*parts[:season_index])) if season_index else "."
    return folder_path, series_folder


def extract_metadata_series_name(file_path: Path) -> str:
    metadata = read_mp4_metadata(file_path)
    if not metadata:
        return ""
    for key in ("tvsh", "\xa9nam", "desc"):
        value = metadata.get(key)
        if value:
            clean = sanitize_name(str(value[0]))
            if clean:
                return clean
    return ""


def load_aliases() -> dict[str, str]:
    ensure_state_dirs()
    if not ALIASES_FILE.exists():
        return {}
    try:
        return json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_aliases(aliases: dict[str, str]) -> None:
    ensure_state_dirs()
    ALIASES_FILE.write_text(
        json.dumps(dict(sorted(aliases.items())), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_aliases_from_instructions(instructions: list[RenameInstruction]) -> dict[str, str]:
    aliases = load_aliases()
    changed: dict[str, str] = {}
    for instruction in instructions:
        key = instruction.series_code.upper()
        value = sanitize_name(instruction.series_name)
        if key and value and aliases.get(key) != value:
            aliases[key] = value
            changed[key] = value
    if changed:
        save_aliases(aliases)
    return changed


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _tmdb_get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    if not TMDB_BEARER_TOKEN:
        raise RuntimeError("TMDB_BEARER_TOKEN ausente.")

    query = urlencode({key: value for key, value in (params or {}).items() if value not in (None, "")})
    url = f"https://api.themoviedb.org/3{path}"
    if query:
        url = f"{url}?{query}"
    return _http_get_json(
        url,
        headers={
            "Authorization": f"Bearer {TMDB_BEARER_TOKEN}",
            "accept": "application/json",
            "User-Agent": "series-renamer/1.0",
        },
    )


def _pick_tmdb_show(results: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    if not results:
        return None

    normalized_query = normalize_text(query)
    best_item: dict[str, Any] | None = None
    best_score = 0.0
    for item in results:
        candidates = [
            item.get("name", ""),
            item.get("original_name", ""),
        ]
        for candidate in candidates:
            normalized_candidate = normalize_text(str(candidate))
            if not normalized_candidate:
                continue
            if normalized_candidate == normalized_query:
                return item
            if normalized_query and normalized_query in normalized_candidate:
                score = 0.95
            elif normalized_candidate in normalized_query:
                score = 0.92
            else:
                score = SequenceMatcher(None, normalized_query, normalized_candidate).ratio()
            if score > best_score:
                best_score = score
                best_item = item
    if best_item and best_score >= 0.45:
        return best_item
    return results[0]


def _query_variants(series_name: str) -> list[str]:
    clean = sanitize_name(series_name)
    if not clean:
        return []

    variants: list[str] = []
    normalized = normalize_text(clean)
    candidates = [clean, normalized]

    tokens = [token for token in normalized.split() if token not in SEARCH_STOPWORDS]
    if tokens:
        candidates.append(" ".join(tokens))
        if len(tokens) > 1:
            candidates.append(" ".join(tokens[:2]))
            candidates.append(" ".join(tokens[-2:]))
        candidates.extend(token for token in tokens if len(token) > 2)

    fallback = titleize_code(clean)
    if fallback:
        candidates.append(fallback)

    for candidate in candidates:
        candidate = sanitize_name(candidate)
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _tmdb_search_candidates(series_name: str, language: str | None = None) -> list[dict[str, Any]]:
    clean_name = sanitize_name(series_name)
    if not clean_name:
        return []

    payloads: list[dict[str, Any]] = []
    variants = _query_variants(clean_name)
    search_languages: list[str | None] = []
    for item in [language, *TMDB_SEARCH_LANGUAGES]:
        if item not in search_languages:
            search_languages.append(item)
    seen: set[tuple[int, str]] = set()

    for variant in variants:
        for query_language in search_languages:
            try:
                payload = _tmdb_get_json(
                    "/search/tv",
                    {
                        "query": variant,
                        "language": tmdb_language_code(query_language),
                        "include_adult": "false",
                    },
                )
            except Exception:
                continue

            results = payload.get("results", []) if isinstance(payload, dict) else []
            for item in results:
                item_id = int(item.get("id", 0) or 0)
                if not item_id:
                    continue
                key = (item_id, str(item.get("name", "")))
                if key in seen:
                    continue
                seen.add(key)
                payloads.append(item)
    return payloads


def _tmdb_search_show(series_name: str, language: str | None = None) -> dict[str, Any] | None:
    clean_name = sanitize_name(series_name)
    if not clean_name:
        return None

    results = _tmdb_search_candidates(clean_name, language=language)
    if results:
        return _pick_tmdb_show(results, clean_name)
    return None


def _episode_title_from_payload(payload: Any, language: str | None = None) -> str:
    if not isinstance(payload, dict):
        return ""

    if (language or "").strip().lower() == "original":
        return sanitize_name(payload.get("original_name", "") or payload.get("name", ""))

    title = sanitize_name(payload.get("name", ""))
    if title:
        return title

    original = sanitize_name(payload.get("original_name", ""))
    if original:
        return original

    return ""


def _tmdb_fetch_season(show_id: int, season: int, language: str | None = None) -> dict[tuple[int, int], str]:
    try:
        payload = _tmdb_get_json(
            f"/tv/{show_id}/season/{season}",
            {"language": tmdb_language_code(language)},
        )
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    mapped: dict[tuple[int, int], str] = {}
    for episode in payload.get("episodes", []) or []:
        number = int(episode.get("episode_number", 0) or episode.get("number", 0) or 0)
        if not number:
            continue
        title = _episode_title_from_payload(episode, language=language)
        if title:
            mapped[(season, number)] = title

    if mapped:
        return mapped

    try:
        translations = _tmdb_get_json(
            f"/tv/{show_id}/season/{season}/translations",
            None,
        )
    except Exception:
        return {}

    items = translations.get("translations", []) if isinstance(translations, dict) else translations
    target_lang = tmdb_language_code(language) or ""
    target_base = target_lang.split("-")[0] if target_lang else ""
    fallback: dict[int, str] = {}
    for item in items or []:
        iso = str(item.get("iso_639_1", "")).lower()
        if target_base and iso != target_base:
            continue
        data = item.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {}
        if isinstance(data, dict):
            translated = sanitize_name(data.get("title", "") or data.get("name", ""))
            if translated:
                number = int(item.get("episode_number", 0) or 0)
                if number:
                    fallback[number] = translated

    return {(season, number): title for number, title in fallback.items()}


def _tmdb_episode_titles(show_id: int, seasons: set[int], language: str | None = None) -> dict[tuple[int, int], str]:
    mapped: dict[tuple[int, int], str] = {}
    for season in sorted(seasons):
        mapped.update(_tmdb_fetch_season(show_id, season, language=language))
    return mapped


def _tvmaze_episode_titles(series_name: str, seasons: set[int]) -> dict[tuple[int, int], str]:
    clean_name = sanitize_name(series_name)
    if not clean_name:
        return {}

    search_url = f"https://api.tvmaze.com/search/shows?q={quote(clean_name)}"
    try:
        with urlopen(Request(search_url, headers={"User-Agent": "series-renamer/1.0"}), timeout=8) as response:
            search_results = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}

    if not isinstance(search_results, list) or not search_results:
        return {}

    candidates: list[tuple[float, dict[str, Any]]] = []
    normalized_query = normalize_text(clean_name)
    for item in search_results:
        show = item.get("show") if isinstance(item, dict) else None
        if not isinstance(show, dict):
            continue
        score = 0.0
        for candidate in [show.get("name", ""), show.get("officialSite", ""), show.get("network", {}).get("name", "") if isinstance(show.get("network"), dict) else ""]:
            normalized_candidate = normalize_text(str(candidate))
            if not normalized_candidate:
                continue
            if normalized_candidate == normalized_query:
                score = 1.0
                break
            ratio = SequenceMatcher(None, normalized_query, normalized_candidate).ratio()
            score = max(score, ratio)
        score += float(item.get("score", 0) or 0) / 100.0
        candidates.append((score, show))

    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, show in candidates[:5]:
        show_id = int(show.get("id", 0) or 0)
        if not show_id:
            continue
        try:
            with urlopen(
                Request(f"https://api.tvmaze.com/shows/{show_id}/episodes", headers={"User-Agent": "series-renamer/1.0"}),
                timeout=8,
            ) as response:
                episodes = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue

        mapped: dict[tuple[int, int], str] = {}
        for episode in episodes if isinstance(episodes, list) else []:
            season = int(episode.get("season", 0) or 0)
            number = int(episode.get("number", 0) or 0)
            if season in seasons and number:
                title = sanitize_name(episode.get("name", ""))
                if title:
                    mapped[(season, number)] = title
        if mapped:
            return mapped

    return {}


def candidate_from_patterns(path: Path) -> tuple[str, re.Match[str]] | None:
    for pattern in RAW_PATTERNS:
        match = pattern.match(path.stem)
        if match:
            return pattern.pattern, match
    return None


def episode_details(path: Path) -> tuple[str, re.Match[str], bool] | None:
    """Read both incoming names and names already in the Radarr/Sonarr style."""
    raw = candidate_from_patterns(path)
    if raw:
        return raw[0], raw[1], False
    organized = ORGANIZED_EPISODE_PATTERN.match(path.stem)
    if organized:
        return ORGANIZED_EPISODE_PATTERN.pattern, organized, True
    return None


def looks_organized(path: Path) -> bool:
    if any(part.lower().startswith("season ") for part in path.parts):
        return True
    return any(pattern.match(path.stem) for pattern in ORGANIZED_PATTERNS)


def folder_hint_for_path(path: Path, root: Path) -> str:
    parts = list(path.parent.relative_to(root).parts)
    for part in reversed(parts):
        clean = sanitize_name(part)
        if clean and clean.lower() not in GENERIC_FOLDER_NAMES and not looks_generic_label(clean):
            return clean
    return ""


def find_episode_match(path: Path) -> tuple[str, re.Match[str]] | None:
    if looks_organized(path):
        return None
    matched = candidate_from_patterns(path)
    if not matched:
        return None
    _, match = matched
    if looks_generic_label(match.group("code")):
        return None
    return matched


def scan_media(root: Path, *, filter_text: str = "") -> list[EpisodeGroup]:
    aliases = load_aliases()
    grouped: dict[str, list[EpisodeCandidate]] = defaultdict(list)
    text_filter = filter_text.strip().lower()

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        matched = find_episode_match(path)
        if not matched:
            continue

        pattern_name, match = matched
        series_code = match.group("code").upper()
        folder_hint = folder_hint_for_path(path, root)
        guessed_series_name = aliases.get(series_code) or folder_hint or titleize_code(series_code)
        if looks_generic_label(guessed_series_name):
            guessed_series_name = titleize_code(series_code)
        relative_dir = str(path.parent.relative_to(root))

        search_blob = " ".join(
            [series_code, guessed_series_name, folder_hint, relative_dir, path.name]
        ).lower()
        if text_filter and text_filter not in search_blob:
            continue

        grouped[series_code].append(
            EpisodeCandidate(
                source_path=path.resolve(),
                relative_dir=relative_dir,
                filename=path.name,
                extension=path.suffix.lower(),
                series_code=series_code,
                season=int(match.group("season")),
                episode=int(match.group("episode")),
                guessed_series_name=guessed_series_name,
                suggested_episode_title="",
                folder_hint=folder_hint,
                pattern_name=pattern_name,
            )
        )

    results: list[EpisodeGroup] = []
    for series_code, entries in grouped.items():
        ordered = sorted(entries, key=lambda item: (item.season, item.episode, item.filename))
        guessed_name = aliases.get(series_code) or ordered[0].folder_hint or titleize_code(series_code)
        if looks_generic_label(guessed_name):
            guessed_name = titleize_code(series_code)
        results.append(
            EpisodeGroup(
                series_code=series_code,
                guessed_name=guessed_name,
                count=len(ordered),
                seasons=sorted({item.season for item in ordered}),
                folder_hints=sorted({item.folder_hint for item in ordered if item.folder_hint}),
                entries=ordered,
            )
        )

    return sorted(results, key=lambda item: item.series_code)


def _folder_display_name(folder_name: str, aliases: dict[str, str]) -> str:
    clean = sanitize_name(folder_name)
    if not clean:
        return folder_name
    alias = aliases.get(clean.upper()) or aliases.get(clean.lower()) or aliases.get(clean)
    if alias and not looks_generic_label(alias):
        return sanitize_name(alias)
    if looks_generic_label(clean):
        return clean
    return clean


def _series_match_blob(*values: str) -> str:
    return " ".join(sanitize_name(value) for value in values if value).lower()


def _relative_parts(relative_path: str) -> tuple[str, ...]:
    clean = (relative_path or "").strip()
    if not clean or clean == ".":
        return ()
    return tuple(part for part in Path(clean).parts if part not in ("", "."))


def _path_anchor(parts: tuple[str, ...]) -> str:
    if not parts:
        return "dir-root"
    cleaned = sanitize_name(" ".join(parts)).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return f"dir-{cleaned or 'root'}"


def _build_directory_tree(groups: list[Any], empty_folders: list[EmptyFolderEntry]) -> DirectoryTreeNode:
    root = DirectoryTreeNode(
        path="",
        name="Raiz",
        anchor_id="dir-root",
        file_count=0,
        series_count=0,
        empty_count=0,
        children=[],
        series_groups=[],
    )
    nodes: dict[tuple[str, ...], DirectoryTreeNode] = {(): root}

    def ensure_node(parts: tuple[str, ...]) -> DirectoryTreeNode:
        if parts in nodes:
            return nodes[parts]

        parent = ensure_node(parts[:-1])
        node = DirectoryTreeNode(
            path="/".join(parts),
            name=parts[-1],
            anchor_id=_path_anchor(parts),
            file_count=0,
            series_count=0,
            empty_count=0,
            children=[],
            series_groups=[],
        )
        nodes[parts] = node
        parent.children.append(node)
        parent.children.sort(key=lambda item: item.name.casefold())
        return node

    for group in groups:
        parts = _relative_parts(group.folder_path)
        node = ensure_node(parts)
        node.file_count += group.count
        node.series_count += 1
        node.series_groups.append(group)
        for depth in range(len(parts)):
            ancestor = ensure_node(parts[:depth])
            ancestor.file_count += group.count
            ancestor.series_count += 1

    for empty in empty_folders:
        parts = _relative_parts(empty.relative_path)
        node = ensure_node(parts)
        node.empty_count += 1
        for depth in range(len(parts)):
            ancestor = ensure_node(parts[:depth])
            ancestor.empty_count += 1

    return root


def scan_library(root: Path, *, filter_text: str = "") -> dict[str, Any]:
    aliases = load_aliases()
    text_filter = sanitize_name(filter_text).lower()
    folder_groups: dict[str, dict[str, Any]] = {}
    pattern_groups: dict[tuple[str, str], dict[str, Any]] = {}
    metadata_mismatches: list[MetadataMismatchEntry] = []
    video_files: list[Path] = []

    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            video_files.append(path)

    for path in video_files:
        matched = episode_details(path)
        if not matched:
            continue

        pattern_name, match, organized = matched
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        folder_path, series_folder = extract_series_folder(path, root)
        display_name = _folder_display_name(series_folder, aliases)
        metadata_series_name = extract_metadata_series_name(path)
        relative_dir = str(path.parent.relative_to(root))
        blob = _series_match_blob(
            series_folder,
            display_name,
            metadata_series_name,
            relative_dir,
            path.name,
        )
        if text_filter and text_filter not in blob:
            continue

        folder_group = folder_groups.setdefault(
            folder_path,
            {
                "folder_path": folder_path,
                "folder_name": series_folder,
                "display_name": display_name,
                "anchor_id": f"series-{_path_anchor(_relative_parts(folder_path))}",
                "count": 0,
                "seasons": set(),
                "entries": [],
            },
        )
        folder_group["count"] += 1
        folder_group["seasons"].add(season)
        folder_group["entries"].append(
            FolderFileEntry(
                source_path=path.resolve(),
                relative_dir=relative_dir,
                filename=path.name,
                extension=path.suffix.lower(),
                series_folder=series_folder,
                season=season,
                episode=episode,
                organized=organized,
                pattern_name=pattern_name,
                metadata_series_name=metadata_series_name,
                metadata_matches_folder=not metadata_series_name
                or normalize_text(metadata_series_name) == normalize_text(display_name),
            )
        )

        if not organized:
            raw_group = pattern_groups.setdefault(
                (folder_path, match.group("code").upper()),
                {
                    "folder_path": folder_path,
                    "series_code": match.group("code").upper(),
                    "guessed_name": display_name,
                    "anchor_id": f"pattern-{_path_anchor(_relative_parts(folder_path))}-{match.group('code').upper()}",
                    "count": 0,
                    "seasons": set(),
                    "entries": [],
                },
            )
            raw_group["count"] += 1
            raw_group["seasons"].add(season)
            raw_group["entries"].append(
                EpisodeCandidate(
                    source_path=path.resolve(),
                    relative_dir=relative_dir,
                    filename=path.name,
                    extension=path.suffix.lower(),
                    series_code=match.group("code").upper(),
                    season=season,
                    episode=episode,
                    guessed_series_name=display_name,
                    suggested_episode_title="",
                    folder_hint=series_folder,
                    pattern_name=pattern_name,
                )
            )

        if metadata_series_name and normalize_text(metadata_series_name) != normalize_text(display_name):
            metadata_mismatches.append(
                MetadataMismatchEntry(
                    source_path=path.resolve(),
                    relative_dir=relative_dir,
                    filename=path.name,
                    folder_series_name=display_name,
                    metadata_series_name=metadata_series_name,
                    season=season,
                    episode=episode,
                    pattern_name=pattern_name,
                    anchor_id=f"metadata-{_path_anchor(_relative_parts(relative_dir))}-{sanitize_name(path.stem).lower()}",
                )
            )

    empty_folders: list[EmptyFolderEntry] = []
    folder_paths: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            folder_paths.append(path)

    for directory in folder_paths:
        try:
            has_entries = any(directory.iterdir())
        except PermissionError:
            continue
        if has_entries:
            continue
        relative_path = str(directory.relative_to(root))
        clean_name = sanitize_name(directory.name)
        if text_filter and text_filter not in _series_match_blob(relative_path, clean_name):
            continue
        empty_folders.append(
            EmptyFolderEntry(
                path=str(directory.resolve()),
                relative_path=relative_path,
                folder_name=directory.name,
                anchor_id=f"empty-{_path_anchor(_relative_parts(relative_path))}",
                depth=len(directory.relative_to(root).parts),
            )
        )

    folder_result = [
        FolderGroup(
            folder_path=item["folder_path"],
            folder_name=item["folder_name"],
            display_name=item["display_name"],
            anchor_id=item["anchor_id"],
            count=item["count"],
            seasons=sorted(item["seasons"]),
            entries=sorted(item["entries"], key=lambda entry: (entry.season, entry.episode, entry.filename)),
        )
        for item in sorted(folder_groups.values(), key=lambda value: (value["folder_path"], value["folder_name"]))
    ]

    pattern_result = [
        PatternGroup(
            folder_path=item["folder_path"],
            series_code=item["series_code"],
            guessed_name=item["guessed_name"],
            anchor_id=item["anchor_id"],
            count=item["count"],
            seasons=sorted(item["seasons"]),
            entries=sorted(item["entries"], key=lambda entry: (entry.season, entry.episode, entry.filename)),
        )
        for item in sorted(pattern_groups.values(), key=lambda value: (value["folder_path"], value["series_code"]))
    ]

    directory_tree = _build_directory_tree(folder_result, empty_folders)
    pattern_directory_tree = _build_directory_tree(pattern_result, empty_folders)

    return {
        "series_groups": folder_result,
        "pattern_groups": pattern_result,
        "empty_folders": sorted(empty_folders, key=lambda item: (item.depth, item.relative_path)),
        "metadata_mismatches": sorted(metadata_mismatches, key=lambda item: (item.relative_dir, item.filename)),
        "directory_tree": directory_tree,
        "pattern_directory_tree": pattern_directory_tree,
        "aliases": load_aliases(),
    }


def build_target_filename(series_name: str, season: int, episode: int, episode_title: str, extension: str) -> str:
    base = f"{sanitize_name(series_name)} - S{season:02d}E{episode:02d}"
    clean_episode_title = sanitize_name(episode_title)
    if clean_episode_title:
        base = f"{base} - {clean_episode_title}"
    return f"{base}{extension.lower()}"


def read_mp4_metadata(file_path: Path) -> dict[str, list[Any]] | None:
    if MP4 is None:
        return None
    if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return None
    try:
        media = MP4(file_path)
    except MP4StreamInfoError:
        return None
    return {key: list(value) for key, value in media.tags.items()} if media.tags else {}


def write_mp4_metadata(file_path: Path, series_name: str, season: int, episode: int, episode_title: str) -> None:
    if MP4 is None:
        return
    if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return

    try:
        media = MP4(file_path)
    except MP4StreamInfoError:
        return

    title = episode_title.strip() or f"{series_name} - S{season:02d}E{episode:02d}"
    media["\xa9nam"] = [title]
    media["tvsh"] = [series_name]
    media["tven"] = [f"S{season:02d}E{episode:02d}"]
    media["tvsn"] = [season]
    media["tves"] = [episode]
    media["stik"] = [10]
    media["desc"] = [title]
    media.save()


def restore_mp4_metadata(file_path: Path, metadata: dict[str, list[Any]] | None) -> None:
    if MP4 is None:
        return
    if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return
    try:
        media = MP4(file_path)
    except MP4StreamInfoError:
        return

    media.delete()
    if metadata:
        for key, value in metadata.items():
            media[key] = value
    media.save()


def write_mp4_series_name(file_path: Path, series_name: str) -> None:
    if MP4 is None:
        return
    if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return

    try:
        media = MP4(file_path)
    except MP4StreamInfoError:
        return

    clean_series_name = sanitize_name(series_name)
    if not clean_series_name:
        return

    media["tvsh"] = [clean_series_name]
    media.save()


def build_preview(instructions: list[RenameInstruction]) -> dict[str, Any]:
    preview_items: list[dict[str, Any]] = []
    occupied_targets: set[Path] = set()
    errors: list[str] = []

    for instruction in instructions:
        clean_series_name = sanitize_name(instruction.series_name)
        if not clean_series_name:
            errors.append(f"{instruction.source_path.name}: nome da série vazio.")
            continue

        season_dir = instruction.destination_root / clean_series_name / f"Season {instruction.season:02d}"
        target_path = season_dir / build_target_filename(
            clean_series_name,
            instruction.season,
            instruction.episode,
            instruction.episode_title,
            instruction.source_path.suffix,
        )

        conflict = ""
        if target_path in occupied_targets:
            conflict = "Conflito dentro do lote."
        elif target_path.exists() and instruction.source_path.resolve() != target_path.resolve():
            conflict = "Arquivo de destino já existe."

        occupied_targets.add(target_path)
        preview_items.append(
            {
                "source": str(instruction.source_path),
                "source_name": instruction.source_path.name,
                "target": str(target_path),
                "series_code": instruction.series_code,
                "series_name": clean_series_name,
                "season": instruction.season,
                "episode": instruction.episode,
                "episode_title": sanitize_name(instruction.episode_title),
                "conflict": conflict,
            }
        )
        if conflict:
            errors.append(f"{instruction.source_path.name}: {conflict}")

    return {
        "items": preview_items,
        "count": len(preview_items),
        "errors": errors,
    }


def _history_file(batch_id: str) -> Path:
    ensure_state_dirs()
    return HISTORY_DIR / f"{batch_id}.json"


def save_history(batch: dict[str, Any]) -> None:
    _history_file(batch["batch_id"]).write_text(
        json.dumps(batch, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_history(limit: int = 8) -> list[dict[str, Any]]:
    ensure_state_dirs()
    items: list[dict[str, Any]] = []
    paths = sorted(HISTORY_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items.append(payload)
        if len(items) >= limit:
            break
    return items


def load_history(batch_id: str) -> dict[str, Any] | None:
    path = _history_file(batch_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def apply_changes(instructions: list[RenameInstruction], *, dry_run: bool = False) -> dict[str, Any]:
    preview = build_preview(instructions)
    messages: dict[str, Any] = {
        "updated": [],
        "skipped": [],
        "errors": list(preview["errors"]),
        "preview": preview["items"],
        "history_batch_id": None,
        "integrations": [],
        "aliases_updated": {},
    }

    if dry_run:
        return messages

    history_ops: list[dict[str, Any]] = []
    preview_by_source = {item["source"]: item for item in preview["items"]}
    for instruction in instructions:
        item = preview_by_source.get(str(instruction.source_path))
        if not item or item["conflict"]:
            continue

        clean_series_name = item["series_name"]
        source_path = instruction.source_path.resolve()
        target_path = Path(item["target"])

        try:
            previous_metadata = read_mp4_metadata(source_path)
            if source_path != target_path.resolve():
                safe_move(source_path, target_path)
            write_mp4_metadata(
                target_path,
                clean_series_name,
                instruction.season,
                instruction.episode,
                instruction.episode_title,
            )
            messages["updated"].append(f"{source_path.name} -> {target_path}")
            history_ops.append(
                {
                    "source": str(source_path),
                    "target": str(target_path),
                    "previous_metadata": previous_metadata,
                    "new_metadata": {
                        "series_name": clean_series_name,
                        "season": instruction.season,
                        "episode": instruction.episode,
                        "episode_title": sanitize_name(instruction.episode_title),
                    },
                }
            )
        except Exception as exc:  # pragma: no cover
            messages["errors"].append(f"{source_path.name}: {exc}")

    messages["aliases_updated"] = update_aliases_from_instructions(instructions)
    if history_ops:
        batch_id = uuid.uuid4().hex[:12]
        payload = {
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "operation_count": len(history_ops),
            "operations": history_ops,
        }
        save_history(payload)
        messages["history_batch_id"] = batch_id
        messages["integrations"] = trigger_integrations()

    return messages


def apply_metadata_changes(
    instructions: list[RenameInstruction],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    messages: dict[str, Any] = {
        "updated": [],
        "skipped": [],
        "errors": [],
        "preview": [],
        "history_batch_id": None,
        "integrations": [],
    }

    if dry_run:
        return messages

    history_ops: list[dict[str, Any]] = []
    for instruction in instructions:
        source_path = instruction.source_path.resolve()
        if not source_path.exists():
            messages["errors"].append(f"{source_path.name}: arquivo ausente.")
            continue

        try:
            previous_metadata = read_mp4_metadata(source_path)
            write_mp4_series_name(source_path, instruction.series_name)
            messages["updated"].append(f"{source_path.name}: metadata da série ajustado para {instruction.series_name}")
            history_ops.append(
                {
                    "type": "metadata",
                    "path": str(source_path),
                    "previous_metadata": previous_metadata,
                    "new_metadata": {
                        "series_name": sanitize_name(instruction.series_name),
                    },
                }
            )
        except Exception as exc:  # pragma: no cover
            messages["errors"].append(f"{source_path.name}: {exc}")

    if history_ops:
        batch_id = uuid.uuid4().hex[:12]
        payload = {
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "operation_count": len(history_ops),
            "operations": history_ops,
        }
        save_history(payload)
        messages["history_batch_id"] = batch_id
        messages["integrations"] = trigger_integrations()

    return messages


def delete_empty_folders(folders: list[Path], *, dry_run: bool = False) -> dict[str, Any]:
    messages: dict[str, Any] = {
        "updated": [],
        "errors": [],
        "preview": [],
        "history_batch_id": None,
        "integrations": [],
    }
    if dry_run:
        messages["preview"] = [str(folder) for folder in folders]
        return messages

    history_ops: list[dict[str, Any]] = []
    for folder in folders:
        try:
            resolved = folder.resolve()
            if not any(root == resolved or root in resolved.parents for root in ALLOWED_ROOTS):
                messages["errors"].append(outside_allowed_roots_message(resolved))
                continue
            if not resolved.exists():
                messages["errors"].append(f"{resolved}: pasta ausente.")
                continue
            if any(resolved.iterdir()):
                messages["errors"].append(f"{resolved}: pasta não está vazia.")
                continue
            resolved.rmdir()
            messages["updated"].append(f"Pasta vazia removida: {resolved}")
            history_ops.append(
                {
                    "type": "empty_folder_delete",
                    "path": str(resolved),
                }
            )
        except Exception as exc:  # pragma: no cover
            messages["errors"].append(f"{folder}: {exc}")

    if history_ops:
        batch_id = uuid.uuid4().hex[:12]
        payload = {
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "operation_count": len(history_ops),
            "operations": history_ops,
        }
        save_history(payload)
        messages["history_batch_id"] = batch_id
        messages["integrations"] = trigger_integrations()

    return messages


def delete_media_items(items: list[Path], *, dry_run: bool = False) -> dict[str, Any]:
    """Remove explicitly selected media, or directories only when empty."""
    messages: dict[str, Any] = {
        "updated": [],
        "errors": [],
        "preview": [],
        "history_batch_id": None,
        "integrations": [],
    }
    for item in items:
        resolved = item.resolve()
        if not any(root != resolved and root in resolved.parents for root in ALLOWED_ROOTS):
            messages["errors"].append(outside_allowed_roots_message(resolved))
            continue
        if not resolved.exists():
            messages["errors"].append(f"{resolved}: item não encontrado.")
            continue
        if dry_run:
            messages["preview"].append(str(resolved))
            continue
        try:
            if resolved.is_dir():
                if any(resolved.iterdir()):
                    messages["errors"].append(f"{resolved}: pasta não está vazia.")
                    continue
                resolved.rmdir()
                messages["updated"].append(f"Pasta removida: {resolved}")
            else:
                resolved.unlink()
                messages["updated"].append(f"Arquivo removido: {resolved}")
        except Exception as exc:  # pragma: no cover
            messages["errors"].append(f"{resolved}: {exc}")
    if messages["updated"]:
        messages["integrations"] = trigger_integrations()
    return messages


def undo_batch(batch_id: str) -> dict[str, list[str]]:
    payload = load_history(batch_id)
    result = {"updated": [], "errors": []}
    if not payload:
        result["errors"].append("Lote não encontrado.")
        return result

    for operation in reversed(payload.get("operations", [])):
        label = operation.get("target") or operation.get("path") or "operação"
        try:
            op_type = operation.get("type", "rename")
            if op_type == "empty_folder_delete":
                folder = Path(operation["path"])
                resolved_folder = folder.resolve()
                if not any(root == resolved_folder or root in resolved_folder.parents for root in ALLOWED_ROOTS):
                    result["errors"].append(outside_allowed_roots_message(resolved_folder))
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                result["updated"].append(f"Pasta restaurada: {folder}")
                continue

            if op_type == "metadata":
                target = Path(operation["path"])
                if not any(root == target.resolve() or root in target.resolve().parents for root in ALLOWED_ROOTS):
                    result["errors"].append(outside_allowed_roots_message(target.resolve()))
                    continue
                if not target.exists():
                    result["errors"].append(f"Arquivo ausente para desfazer metadados: {target}")
                    continue
                restore_mp4_metadata(target, operation.get("previous_metadata"))
                result["updated"].append(f"Metadata restaurado: {target}")
                continue

            target = Path(operation["target"])
            source = Path(operation["source"])
            resolved_target = target.resolve()
            resolved_source = source.resolve()
            if not any(root == resolved_target or root in resolved_target.parents for root in ALLOWED_ROOTS):
                result["errors"].append(outside_allowed_roots_message(resolved_target))
                continue
            if not any(root == resolved_source or root in resolved_source.parents for root in ALLOWED_ROOTS):
                result["errors"].append(outside_allowed_roots_message(resolved_source))
                continue
            if not target.exists():
                result["errors"].append(f"Arquivo ausente para desfazer: {target}")
                continue
            if resolved_target == resolved_source:
                restore_mp4_metadata(source, operation.get("previous_metadata"))
                result["updated"].append(f"{target.name} -> {source}")
                continue
            try:
                safe_move(target, source)
            except FileExistsError:
                result["errors"].append(f"Destino já existe durante desfazer: {source}")
                continue
            restore_mp4_metadata(source, operation.get("previous_metadata"))
            result["updated"].append(f"{target.name} -> {source}")
        except Exception as exc:  # pragma: no cover
            result["errors"].append(f"{label}: {exc}")
    return result


def fetch_episode_titles(series_name: str, seasons: set[int], language: str | None = None) -> dict[tuple[int, int], str]:
    series_name = sanitize_name(series_name)
    if not series_name:
        return {}

    for candidate_name in _query_variants(series_name):
        show = _tmdb_search_show(candidate_name, language=language)
        if show:
            show_id = int(show.get("id", 0) or 0)
            if show_id:
                mapped = _tmdb_episode_titles(show_id, seasons, language=language)
                if mapped:
                    return mapped

    fallback = _tvmaze_episode_titles(series_name, seasons)
    if fallback:
        return fallback

    return {}


def apply_online_titles(instructions: list[RenameInstruction], language: str | None = None) -> dict[str, Any]:
    grouped: dict[str, list[RenameInstruction]] = defaultdict(list)
    for item in instructions:
        grouped[sanitize_name(item.series_name)].append(item)

    applied = 0
    messages: list[str] = []
    for series_name, items in grouped.items():
        season_set = {item.season for item in items}
        episode_map = fetch_episode_titles(series_name, season_set, language=language)
        if not episode_map:
            messages.append(f"Nenhum título encontrado online para {series_name}.")
            continue
        for item in items:
            title = episode_map.get((item.season, item.episode), "")
            if title:
                if item.episode_title.strip() != title:
                    item.episode_title = title
                    applied += 1
        messages.append(f"{series_name}: {len(episode_map)} episódios consultados.")
    return {"applied": applied, "messages": messages}


def apply_online_titles_to_groups(groups: list[EpisodeGroup], language: str | None = None) -> dict[str, Any]:
    applied = 0
    messages: list[str] = []
    for group in groups:
        series_name = sanitize_name(group.guessed_name)
        if not series_name or looks_generic_label(series_name):
            messages.append(f"{group.series_code}: nome da série genérico, títulos online ignorados.")
            continue

        season_set = set(group.seasons)
        episode_map = fetch_episode_titles(series_name, season_set, language=language)
        if not episode_map:
            messages.append(f"Nenhum título encontrado online para {series_name}.")
            continue

        for item in group.entries:
            title = episode_map.get((item.season, item.episode), "")
            if title:
                if item.suggested_episode_title.strip() != title:
                    item.suggested_episode_title = title
                    applied += 1
        messages.append(f"{series_name}: {len(episode_map)} episódios consultados.")

    return {"applied": applied, "messages": messages}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=8):
        return


def _post_empty(url: str, headers: dict[str, str]) -> None:
    request = Request(url, data=b"{}", headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=8):
        return


def trigger_integrations() -> list[str]:
    messages: list[str] = []

    if SONARR_URL and SONARR_API_KEY:
        try:
            _post_json(
                f"{SONARR_URL.rstrip('/')}/api/v3/command",
                {"name": "RescanSeries"},
                {"X-Api-Key": SONARR_API_KEY},
            )
            messages.append("Sonarr: reescaneamento solicitado.")
        except Exception:
            messages.append("Sonarr: falha ao solicitar reescaneamento.")

    if JELLYFIN_URL and JELLYFIN_API_KEY and JELLYFIN_LIBRARY_ID:
        try:
            _post_empty(
                f"{JELLYFIN_URL.rstrip('/')}/Items/{JELLYFIN_LIBRARY_ID}/Refresh",
                {"X-Emby-Token": JELLYFIN_API_KEY},
            )
            messages.append("Jellyfin: atualização da biblioteca solicitada.")
        except Exception:
            messages.append("Jellyfin: falha ao solicitar atualização.")

    return messages


def parse_form_instructions(
    *,
    source_root: Path,
    destination_root: Path,
    file_path: list[str],
    selected_paths: set[str] | None = None,
    series_code: list[str],
    series_name: list[str],
    season: list[str],
    episode: list[str],
    episode_title: list[str],
) -> list[RenameInstruction]:
    instructions: list[RenameInstruction] = []
    for index, raw_path in enumerate(file_path):
        resolved = Path(raw_path).resolve()
        if not (resolved == source_root or source_root in resolved.parents):
            continue
        if selected_paths is not None and str(resolved) not in selected_paths:
            continue

        instructions.append(
            RenameInstruction(
                source_path=resolved,
                source_root=source_root,
                destination_root=destination_root,
                series_code=series_code[index].strip().upper(),
                series_name=series_name[index].strip(),
                season=int(season[index]),
                episode=int(episode[index]),
                episode_title=episode_title[index].strip(),
            )
        )
    return instructions


def filter_instructions_by_group(instructions: list[RenameInstruction], series_code: str) -> list[RenameInstruction]:
    clean_code = sanitize_name(series_code).upper()
    if not clean_code:
        return []
    return [item for item in instructions if item.series_code.upper() == clean_code]


def serialize_groups(groups: list[EpisodeGroup]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for group in groups:
        payload.append(
            {
                "series_code": group.series_code,
                "guessed_name": group.guessed_name,
                "count": group.count,
                "seasons": group.seasons,
                "folder_hints": group.folder_hints,
                "entries": [asdict(entry) for entry in group.entries],
            }
        )
    return payload
