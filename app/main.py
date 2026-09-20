from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import services
from .services import (
    apply_changes,
    apply_metadata_changes,
    delete_empty_folders,
    delete_media_items,
    fetch_episode_titles,
    list_history,
    load_aliases,
    parse_form_instructions,
    scan_library,
    undo_batch,
    TMDB_DEFAULT_LANGUAGE,
)

LIBRARIES = [
    {
        "key": "series",
        "label": "Series",
        "scan_root": Path("/media/series"),
        "destination_root": Path("/media/series"),
        "description": "Episódios e temporadas.",
    },
    {
        "key": "movies",
        "label": "Filmes",
        "scan_root": Path("/media/movies"),
        "destination_root": Path("/media/movies"),
        "description": "Biblioteca principal de filmes.",
    },
    {
        "key": "cristaos",
        "label": "Cristaos",
        "scan_root": Path("/media/cristaos"),
        "destination_root": Path("/media/cristaos"),
        "description": "Conteúdo cristão separado.",
    },
    {
        "key": "livros",
        "label": "Livros",
        "scan_root": Path("/media/livros"),
        "destination_root": Path("/media/livros"),
        "description": "Arquivos de livros.",
    },
    {
        "key": "audiolivros",
        "label": "Audiolivros",
        "scan_root": Path("/media/Audio-livros"),
        "destination_root": Path("/media/Audio-livros"),
        "description": "Coleção de áudio-livros.",
    },
]

LIBRARY_MAP = {item["key"]: item for item in LIBRARIES}
DEFAULT_LIBRARY_KEY = "series"
ALLOWED_ROOTS = [item["scan_root"].resolve() for item in LIBRARIES]
services.ALLOWED_ROOTS = ALLOWED_ROOTS
SERIES_LIBRARY_ROOT = LIBRARY_MAP[DEFAULT_LIBRARY_KEY]["scan_root"].resolve()

app = FastAPI(title="Series Renamer", version="3.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_library(library_key: str | None) -> dict:
    return LIBRARY_MAP.get(library_key or DEFAULT_LIBRARY_KEY, LIBRARY_MAP[DEFAULT_LIBRARY_KEY])


def is_allowed_root(path: Path) -> bool:
    return any(path == root or root in path.parents for root in ALLOWED_ROOTS)


def safe_root(path_str: str | None, fallback: Path) -> Path:
    candidate = Path(path_str).expanduser().resolve() if path_str else fallback.resolve()
    if not is_allowed_root(candidate):
        raise ValueError(
            f"Caminho fora das raízes permitidas. Use algo dentro de: "
            f"{', '.join(str(root) for root in ALLOWED_ROOTS)}"
        )
    return candidate


def render_index(
    request: Request,
    *,
    library_key: str,
    scan_root: Path,
    destination_root: Path,
    filter_text: str = "",
    message: str | None = None,
    result: dict | None = None,
    undo_result: dict | None = None,
) -> HTMLResponse:
    current_library = get_library(library_key)
    scan_data = scan_library(scan_root, filter_text=filter_text)
    aliases = load_aliases()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "libraries": LIBRARIES,
            "current_library": current_library,
            "scan_root": str(scan_root),
            "destination_root": str(destination_root),
            "allowed_roots": [str(root) for root in ALLOWED_ROOTS],
            "filter_text": filter_text,
            "message": message,
            "result": result,
            "undo_result": undo_result,
            "history": list_history(),
            "aliases": aliases,
            "series_groups": scan_data["series_groups"],
            "pattern_groups": scan_data["pattern_groups"],
            "empty_folders": scan_data["empty_folders"],
            "metadata_mismatches": scan_data["metadata_mismatches"],
            "unrecognized_groups": scan_data["unrecognized_groups"],
            "directory_tree": scan_data["directory_tree"],
            "pattern_directory_tree": scan_data["pattern_directory_tree"],
            "scan_stats": {
                "series_groups": len(scan_data["series_groups"]),
                "pattern_groups": len(scan_data["pattern_groups"]),
                "empty_folders": len(scan_data["empty_folders"]),
                "metadata_mismatches": len(scan_data["metadata_mismatches"]),
                "unrecognized_groups": len(scan_data["unrecognized_groups"]),
                "aliases": len(aliases),
            },
        },
    )


def _parse_selected_paths(selected_path: list[str]) -> set[str]:
    return {str(Path(path).resolve()) for path in selected_path if path.strip()}


def _build_instructions_from_form(
    *,
    source_root: Path,
    destination_root: Path,
    file_path: list[str],
    selected_path: set[str],
    series_name: list[str],
    season: list[str],
    episode: list[str],
    episode_title: list[str],
) -> list:
    return parse_form_instructions(
        source_root=source_root,
        destination_root=destination_root,
        file_path=file_path,
        selected_paths=selected_path if selected_path else set(),
        series_code=[""] * len(file_path),
        series_name=series_name,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )


@app.post("/group-titles", response_class=JSONResponse)
async def group_titles(
    series_name: Annotated[str, Form()] = "",
    seasons: Annotated[list[str], Form()] = [],
    language: Annotated[str, Form()] = TMDB_DEFAULT_LANGUAGE,
) -> JSONResponse:
    clean_series_name = series_name.strip()
    season_set = {int(value) for value in seasons if value.strip().isdigit()}
    titles = fetch_episode_titles(clean_series_name, season_set, language=language)
    return JSONResponse(
        {
            "series_name": clean_series_name,
            "seasons": sorted(season_set),
            "titles": [
                {"season": season, "episode": episode, "title": title}
                for (season, episode), title in sorted(titles.items())
            ],
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    library: str = DEFAULT_LIBRARY_KEY,
    root: str | None = None,
    destination_root: str | None = None,
    filter_text: str = "",
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        scan_root = safe_root(root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=scan_root,
            destination_root=target_root,
            filter_text=filter_text,
        )

    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )


@app.get("/media")
async def media_file(path: str) -> FileResponse:
    """Serve only media files inside a configured library for in-app playback."""
    candidate = Path(path).expanduser().resolve()
    if not is_allowed_root(candidate) or not candidate.is_file() or candidate.suffix.lower() not in services.VIDEO_EXTENSIONS:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Arquivo de mídia não encontrado")

    media_types = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".m2ts": "video/mp2t",
        ".ts": "video/mp2t",
    }
    return FileResponse(candidate, media_type=media_types.get(candidate.suffix.lower(), "video/mp4"), filename=candidate.name)


@app.post("/apply", response_class=HTMLResponse)
async def apply(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    selected_path: Annotated[list[str], Form()] = [],
    file_path: Annotated[list[str], Form()] = [],
    series_name: Annotated[list[str], Form()] = [],
    season: Annotated[list[str], Form()] = [],
    episode: Annotated[list[str], Form()] = [],
    episode_title: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    selected = _parse_selected_paths(selected_path)
    instructions = _build_instructions_from_form(
        source_root=source_root,
        destination_root=target_root,
        file_path=file_path,
        selected_path=selected,
        series_name=series_name,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )
    if not instructions:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=source_root,
            destination_root=target_root,
            filter_text=filter_text,
            message="Nenhum arquivo foi selecionado para renomear.",
        )

    result = apply_changes(instructions, dry_run=False)
    message = "Arquivos processados com segurança."
    if result.get("history_batch_id"):
        message = f"{message} Lote salvo para desfazer: {result['history_batch_id']}."
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        result=result,
        message=message,
    )


@app.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    selected_path: Annotated[list[str], Form()] = [],
    file_path: Annotated[list[str], Form()] = [],
    series_name: Annotated[list[str], Form()] = [],
    season: Annotated[list[str], Form()] = [],
    episode: Annotated[list[str], Form()] = [],
    episode_title: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    selected = _parse_selected_paths(selected_path)
    instructions = _build_instructions_from_form(
        source_root=source_root,
        destination_root=target_root,
        file_path=file_path,
        selected_path=selected,
        series_name=series_name,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )
    if not instructions:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=source_root,
            destination_root=target_root,
            filter_text=filter_text,
            message="Nenhum arquivo foi selecionado para prévia.",
        )

    result = apply_changes(instructions, dry_run=True)
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        result=result,
        message="Prévia gerada sem alterar arquivos.",
    )


@app.post("/apply-metadata", response_class=HTMLResponse)
async def apply_metadata(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    selected_path: Annotated[list[str], Form()] = [],
    file_path: Annotated[list[str], Form()] = [],
    series_name: Annotated[list[str], Form()] = [],
    season: Annotated[list[str], Form()] = [],
    episode: Annotated[list[str], Form()] = [],
    episode_title: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    selected = _parse_selected_paths(selected_path)
    instructions = _build_instructions_from_form(
        source_root=source_root,
        destination_root=source_root,
        file_path=file_path,
        selected_path=selected,
        series_name=series_name,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )
    if not instructions:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=source_root,
            destination_root=target_root,
            filter_text=filter_text,
            message="Nenhum arquivo foi selecionado para corrigir metadados.",
        )

    result = apply_metadata_changes(instructions, dry_run=False)
    message = "Metadados ajustados com segurança."
    if result.get("history_batch_id"):
        message = f"{message} Lote salvo para desfazer: {result['history_batch_id']}."
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        result=result,
        message=message,
    )


@app.post("/delete-empty-folders", response_class=HTMLResponse)
async def delete_empty_folders_view(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    folder_path: Annotated[list[str], Form()] = [],
    confirm_empty_delete: Annotated[str, Form()] = "",
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    if source_root != SERIES_LIBRARY_ROOT:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=source_root,
            destination_root=target_root,
            filter_text=filter_text,
            message=f"A exclusão de pastas vazias está restrita ao diretório {SERIES_LIBRARY_ROOT}.",
        )

    if confirm_empty_delete.strip().lower() != "yes":
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=source_root,
            destination_root=target_root,
            filter_text=filter_text,
            message="Confirme a exclusão de pastas vazias antes de prosseguir.",
        )

    folders = [source_root / Path(item) for item in folder_path if item.strip()]
    result = delete_empty_folders(folders, dry_run=False)
    message = "Pastas vazias processadas."
    if result.get("history_batch_id"):
        message = f"{message} Lote salvo para desfazer: {result['history_batch_id']}."
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        result=result,
        message=message,
    )


@app.post("/delete-media", response_class=HTMLResponse)
async def delete_media_view(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    selected_path: Annotated[list[str], Form()] = [],
    confirm_delete: Annotated[list[str], Form()] = [],
    delete_confirmation: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(request, library_key=current_library["key"],
                            scan_root=current_library["scan_root"].resolve(),
                            destination_root=current_library["destination_root"].resolve(), message=str(exc))

    confirmed = any(value.strip().lower() == "yes" for value in confirm_delete)
    typed_confirmation = any(value.strip().upper() == "APAGAR" for value in delete_confirmation)
    if not confirmed or not typed_confirmation:
        return render_index(
            request, library_key=current_library["key"], scan_root=source_root,
            destination_root=target_root, filter_text=filter_text,
            message="Exclusão cancelada: marque a confirmação e digite APAGAR exatamente.",
        )

    items = [Path(item).resolve() for item in selected_path if item.strip()]
    if not items:
        return render_index(
            request, library_key=current_library["key"], scan_root=source_root,
            destination_root=target_root, filter_text=filter_text,
            message="Nenhum arquivo ou pasta foi selecionado para apagar.",
        )
    result = delete_media_items(items)
    return render_index(
        request, library_key=current_library["key"], scan_root=source_root,
        destination_root=target_root, filter_text=filter_text, result=result,
        message="Exclusão concluída. Arquivos removidos não podem ser restaurados pelo histórico.",
    )


@app.post("/undo", response_class=HTMLResponse)
async def undo(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    batch_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    result = undo_batch(batch_id.strip())
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        undo_result=result,
        message=f"Desfazer executado para o lote {batch_id.strip()}.",
    )


@app.post("/aliases/save", response_class=HTMLResponse)
async def save_alias(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    alias_key: Annotated[str, Form()] = "",
    alias_value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    from .services import load_aliases, save_aliases

    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    aliases = load_aliases()
    clean_key = alias_key.strip().upper()
    clean_value = alias_value.strip()
    if clean_key and clean_value:
        aliases[clean_key] = clean_value
        save_aliases(aliases)
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        message=f"Alias salvo para {clean_key}." if clean_key and clean_value else "Nenhuma alteração salva.",
    )


@app.post("/aliases/delete", response_class=HTMLResponse)
async def delete_alias(
    request: Request,
    library: Annotated[str, Form()] = DEFAULT_LIBRARY_KEY,
    scan_root: Annotated[str, Form()] = "",
    destination_root: Annotated[str, Form()] = "",
    filter_text: Annotated[str, Form()] = "",
    alias_key: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    from .services import load_aliases, save_aliases

    current_library = get_library(library)
    try:
        source_root = safe_root(scan_root, current_library["scan_root"])
        target_root = safe_root(destination_root, current_library["destination_root"])
    except ValueError as exc:
        return render_index(
            request,
            library_key=current_library["key"],
            scan_root=current_library["scan_root"].resolve(),
            destination_root=current_library["destination_root"].resolve(),
            message=str(exc),
        )

    aliases = load_aliases()
    clean_keys = [key.strip().upper() for key in alias_key if key.strip()]
    removed: list[str] = []
    for clean_key in clean_keys:
        if clean_key in aliases:
            aliases.pop(clean_key, None)
            removed.append(clean_key)
    save_aliases(aliases)
    return render_index(
        request,
        library_key=current_library["key"],
        scan_root=source_root,
        destination_root=target_root,
        filter_text=filter_text,
        message=(
            f"Aliases removidos: {', '.join(removed)}."
            if removed
            else "Nenhum alias removido."
        ),
    )
