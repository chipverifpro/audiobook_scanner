#!/usr/bin/env python3
"""Incrementally scan audiobook files into a portable SQLite catalogue.

The scanner deliberately separates reading from writing: it never modifies an
audio file.  A later tag-write command can use this catalogue's proposed
metadata after it has been reviewed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from mutagen import File as MutagenFile
from mutagen import MutagenError


SCHEMA_VERSION = 5
AUDIO_SUFFIXES = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wma", ".aac"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "catalog.sqlite3"


@dataclass(frozen=True)
class DescriptionOverride:
    source_path: str | None
    size_bytes: int | None
    modified_ns: int | None
    text: str | None


@dataclass(frozen=True)
class SeriesInfo:
    """The optional series-wide information file for an Author - Series folder."""
    info_path: str | None
    is_complete: bool
    text: str | None


@dataclass(frozen=True)
class ScanResult:
    path: Path
    relative_path: str
    book_path: str
    author_folder: str | None
    series_folder: str | None
    cover_image_path: str | None
    description_source_path: str | None
    description_size_bytes: int | None
    description_modified_ns: int | None
    size_bytes: int
    modified_ns: int
    duration_seconds: float | None
    bitrate: int | None
    codec: str | None
    values: dict[str, list[str]]
    raw_tags: dict[str, list[str]]


def _text(value: Any) -> str:
    """Produce a stable, displayable representation of a Mutagen value."""
    return str(value).strip()


def _frame_values(frame: Any) -> list[str]:
    if hasattr(frame, "text"):
        return [_text(item) for item in frame.text if _text(item)]
    if hasattr(frame, "url"):
        return [_text(frame.url)] if _text(frame.url) else []
    if hasattr(frame, "desc"):
        return [_text(frame.desc)] if _text(frame.desc) else []
    value = _text(frame)
    return [value] if value else []


def _first(values: dict[str, list[str]], *keys: str) -> list[str]:
    for key in keys:
        if values.get(key):
            return values[key]
    return []


def _normalise_tags(tags: Any) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (catalogue fields, loss-minimising raw tag map)."""
    raw: dict[str, list[str]] = {}
    if not tags:
        return {}, raw
    for key in tags.keys():
        value = tags[key]
        if isinstance(value, (list, tuple)):
            parts = [_text(item) for item in value if _text(item)]
        else:
            parts = _frame_values(value)
        if parts:
            raw[str(key)] = parts

    # ID3 frame IDs plus common easy-tag names permit MP3 and future formats.
    lower = {key.lower(): value for key, value in raw.items()}
    fields = {
        "title": _first(raw, "TIT2", "title") or lower.get("title", []),
        "album": _first(raw, "TALB", "album") or lower.get("album", []),
        "artist": _first(raw, "TPE1", "artist") or lower.get("artist", []),
        "album_artist": _first(raw, "TPE2", "albumartist", "album artist") or lower.get("albumartist", []),
        "narrator": _first(raw, "TCOM", "composer") or lower.get("composer", []),
        "genre": _first(raw, "TCON", "genre") or lower.get("genre", []),
        "year": _first(raw, "TDRC", "TYER", "date", "year") or lower.get("date", []) or lower.get("year", []),
        "track_number": _first(raw, "TRCK", "tracknumber") or lower.get("tracknumber", []),
        "disc_number": _first(raw, "TPOS", "discnumber") or lower.get("discnumber", []),
        "description": _first(raw, "COMM::eng", "comment") or lower.get("comment", []),
        "series": _first(raw, "MVNM", "series") or lower.get("series", []),
        "series_part": _first(raw, "MVIN", "series-part", "seriespart") or lower.get("series-part", []),
        "language": _first(raw, "TLAN", "language") or lower.get("language", []),
    }
    # Custom TXXX frames are commonly used for audiobook series metadata.
    for key, value in raw.items():
        upper = key.upper()
        if upper.startswith("TXXX:"):
            label = upper[5:].strip()
            if label in {"SERIES", "MVNM"}:
                fields["series"] = value
            elif label in {"SERIES-PART", "SERIES_PART", "MVIN"}:
                fields["series_part"] = value
            elif label in {"NARRATOR", "READER"}:
                fields["narrator"] = value
    return {key: value for key, value in fields.items() if value}, raw


def find_cover_image(book_directory: Path, root: Path) -> str | None:
    """Choose a direct image child of a book folder without reading its bytes."""
    try:
        candidates = [
            path for path in book_directory.iterdir()
            if path.is_file() and not path.name.startswith("._") and path.suffix.lower() in IMAGE_SUFFIXES
        ]
    except OSError:
        return None
    if not candidates:
        return None

    def preference(path: Path) -> tuple[int, int, str]:
        name = path.stem.casefold()
        # Familiar names win; an otherwise arbitrary image is the fallback.
        name_rank = 0 if "cover" in name else 1 if "folder" in name or "front" in name else 2
        format_rank = {".jpg": 0, ".jpeg": 1, ".png": 2, ".webp": 3}[path.suffix.lower()]
        return name_rank, format_rank, name

    return str(min(candidates, key=preference).relative_to(root))


def read_description_override(book_directory: Path, root: Path) -> DescriptionOverride:
    """Read an optional book-folder description.html or description.txt."""
    description_path: Path | None = None
    stat = None
    # HTML wins when both exist because it explicitly expresses formatted content.
    for filename in ("description.html", "description.txt"):
        candidate = book_directory / filename
        try:
            stat = candidate.stat()
            description_path = candidate
            break
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot inspect {filename}: {exc}") from exc
    if description_path is None or stat is None:
        return DescriptionOverride(None, None, None, None)
    try:
        text = description_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot read {description_path.name}: {exc}") from exc
    return DescriptionOverride(
        str(description_path.relative_to(root)), stat.st_size, stat.st_mtime_ns, text,
    )


def read_series_info(series_directory: Path, root: Path) -> SeriesInfo:
    """Find a series.html or series.txt file, including its COMPLETE marker."""
    # HTML wins when both exist, consistent with description-file precedence.
    for filename in ("series.html", "series.txt"):
        candidate = series_directory / filename
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"cannot read {filename}: {exc}") from exc
        first_word = re.search(r"\S+", text)
        return SeriesInfo(
            str(candidate.relative_to(root)),
            bool(first_word and first_word.group(0).casefold() == "complete"),
            text,
        )
    return SeriesInfo(None, False, None)


def scan_file(
    path: Path,
    root: Path,
    cover_image_path: str | None,
    description_override: DescriptionOverride,
) -> ScanResult:
    stat = path.stat()
    relative = path.relative_to(root)
    parts = relative.parts
    # The existing library convention is Author / [Series] / Book / Track.
    # The immediate parent is reliably the book boundary even for deeper trees.
    book_path = str(relative.parent)
    author_folder = parts[0] if len(parts) > 1 else None
    series_folder = parts[1] if len(parts) > 2 else None
    try:
        audio = MutagenFile(path, easy=False)
    except (MutagenError, OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read tags: {exc}") from exc
    if audio is None:
        raise RuntimeError("unsupported or invalid audio file")
    values, raw_tags = _normalise_tags(audio.tags)
    if description_override.text is not None:
        # An empty override intentionally clears a short/incorrect ID3 comment.
        values["description"] = [description_override.text] if description_override.text else []
    info = getattr(audio, "info", None)
    duration = getattr(info, "length", None)
    bitrate = getattr(info, "bitrate", None)
    return ScanResult(
        path=path, relative_path=str(relative), book_path=book_path,
        author_folder=author_folder, series_folder=series_folder, cover_image_path=cover_image_path,
        description_source_path=description_override.source_path,
        description_size_bytes=description_override.size_bytes,
        description_modified_ns=description_override.modified_ns,
        size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns,
        duration_seconds=float(duration) if duration is not None else None,
        bitrate=int(bitrate) if bitrate is not None else None,
        codec=type(audio).__name__, values=values, raw_tags=raw_tags,
    )


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS catalog_sources (
          id INTEGER PRIMARY KEY, root_path TEXT NOT NULL UNIQUE, last_scan_at TEXT
        );
        CREATE TABLE IF NOT EXISTS books (
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES catalog_sources(id),
          book_path TEXT NOT NULL, author_folder TEXT, series_folder TEXT, cover_image_path TEXT,
          UNIQUE(source_id, book_path)
        );
        CREATE TABLE IF NOT EXISTS series_info (
          source_id INTEGER NOT NULL REFERENCES catalog_sources(id),
          series_path TEXT NOT NULL, info_path TEXT, info_text TEXT,
          is_complete INTEGER NOT NULL DEFAULT 0
            CHECK(is_complete IN (0, 1)),
          PRIMARY KEY(source_id, series_path)
        );
        CREATE TABLE IF NOT EXISTS tracks (
          id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
          relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
          duration_seconds REAL, bitrate INTEGER, codec TEXT, values_json TEXT NOT NULL,
          raw_tags_json TEXT NOT NULL, description_source_path TEXT, description_size_bytes INTEGER,
          description_modified_ns INTEGER, scanned_at TEXT NOT NULL,
          error TEXT, UNIQUE(book_id, relative_path)
        );
        CREATE INDEX IF NOT EXISTS idx_books_author_series ON books(author_folder, series_folder);
        CREATE INDEX IF NOT EXISTS idx_tracks_relative_path ON tracks(relative_path);
        CREATE TABLE IF NOT EXISTS scan_errors (
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES catalog_sources(id),
          relative_path TEXT NOT NULL, message TEXT NOT NULL, scanned_at TEXT NOT NULL,
          UNIQUE(source_id, relative_path)
        );
    """)
    book_columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)")}
    if "cover_image_path" not in book_columns:
        connection.execute("ALTER TABLE books ADD COLUMN cover_image_path TEXT")
    track_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tracks)")}
    for column, definition in (
        ("description_source_path", "TEXT"),
        ("description_size_bytes", "INTEGER"),
        ("description_modified_ns", "INTEGER"),
    ):
        if column not in track_columns:
            connection.execute(f"ALTER TABLE tracks ADD COLUMN {column} {definition}")
    series_columns = {row["name"] for row in connection.execute("PRAGMA table_info(series_info)")}
    if "info_text" not in series_columns:
        connection.execute("ALTER TABLE series_info ADD COLUMN info_text TEXT")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return connection


def source_id(connection: sqlite3.Connection, root: Path) -> int:
    root_text = str(root.resolve())
    connection.execute("INSERT OR IGNORE INTO catalog_sources(root_path) VALUES (?)", (root_text,))
    return connection.execute("SELECT id FROM catalog_sources WHERE root_path = ?", (root_text,)).fetchone()[0]


def unchanged(connection: sqlite3.Connection, source: int, result: ScanResult) -> bool:
    row = connection.execute("""
      SELECT t.size_bytes, t.modified_ns, t.description_source_path, t.description_size_bytes,
             t.description_modified_ns FROM tracks t JOIN books b ON b.id = t.book_id
      WHERE b.source_id = ? AND t.relative_path = ?
    """, (source, result.relative_path)).fetchone()
    return bool(
        row and row["size_bytes"] == result.size_bytes and row["modified_ns"] == result.modified_ns
        and row["description_source_path"] == result.description_source_path
        and row["description_size_bytes"] == result.description_size_bytes
        and row["description_modified_ns"] == result.description_modified_ns
    )


def update_book_cover(connection: sqlite3.Connection, source: int, book_path: str, cover_image_path: str | None) -> None:
    connection.execute(
        "UPDATE books SET cover_image_path=? WHERE source_id=? AND book_path=?",
        (cover_image_path, source, book_path),
    )


def store_series_info(
    connection: sqlite3.Connection,
    source: int,
    series_path: str,
    info: SeriesInfo,
) -> None:
    connection.execute("""
      INSERT INTO series_info(source_id, series_path, info_path, info_text, is_complete) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(source_id, series_path) DO UPDATE SET info_path=excluded.info_path,
      info_text=excluded.info_text, is_complete=excluded.is_complete
    """, (source, series_path, info.info_path, info.text, int(info.is_complete)))


def store_result(connection: sqlite3.Connection, source: int, result: ScanResult, now: str) -> None:
    connection.execute("""
      INSERT INTO books(source_id, book_path, author_folder, series_folder, cover_image_path) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(source_id, book_path) DO UPDATE SET author_folder=excluded.author_folder, series_folder=excluded.series_folder,
      cover_image_path=excluded.cover_image_path
    """, (source, result.book_path, result.author_folder, result.series_folder, result.cover_image_path))
    book_id = connection.execute("SELECT id FROM books WHERE source_id=? AND book_path=?", (source, result.book_path)).fetchone()[0]
    connection.execute("""
      INSERT INTO tracks(book_id, relative_path, size_bytes, modified_ns, duration_seconds, bitrate, codec, values_json, raw_tags_json,
                         description_source_path, description_size_bytes, description_modified_ns, scanned_at, error)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
      ON CONFLICT(book_id, relative_path) DO UPDATE SET size_bytes=excluded.size_bytes, modified_ns=excluded.modified_ns,
      duration_seconds=excluded.duration_seconds, bitrate=excluded.bitrate, codec=excluded.codec, values_json=excluded.values_json,
      raw_tags_json=excluded.raw_tags_json, description_source_path=excluded.description_source_path,
      description_size_bytes=excluded.description_size_bytes, description_modified_ns=excluded.description_modified_ns,
      scanned_at=excluded.scanned_at, error=NULL
    """, (book_id, result.relative_path, result.size_bytes, result.modified_ns, result.duration_seconds, result.bitrate, result.codec,
          json.dumps(result.values, ensure_ascii=False, sort_keys=True), json.dumps(result.raw_tags, ensure_ascii=False, sort_keys=True),
          result.description_source_path, result.description_size_bytes, result.description_modified_ns, now))
    connection.execute("DELETE FROM scan_errors WHERE source_id=? AND relative_path=?", (source, result.relative_path))


def audio_files(root: Path) -> Iterable[Path]:
    for candidate in root.rglob("*"):
        if candidate.is_file() and candidate.suffix.lower() in AUDIO_SUFFIXES and not candidate.name.startswith("._"):
            yield candidate


def report_progress(totals: dict[str, int]) -> None:
    """Show useful scan progress without reopening any source files."""
    print(
        "Progress — tracks: {found} seen, {scanned} scanned, {unchanged} unchanged, "
        "{errors} errors; books: {books}.".format(**totals),
        flush=True,
    )


def remove_missing_entries(
    connection: sqlite3.Connection, source: int, seen_track_paths: set[str],
) -> tuple[int, int, int]:
    """Remove stale catalogue rows for one fully scanned source root."""
    connection.execute("CREATE TEMP TABLE scan_seen_track_paths (relative_path TEXT PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO scan_seen_track_paths(relative_path) VALUES (?)",
        ((path,) for path in seen_track_paths),
    )
    removed_tracks = connection.execute("""
      DELETE FROM tracks
      WHERE book_id IN (SELECT id FROM books WHERE source_id=?)
        AND NOT EXISTS (
          SELECT 1 FROM scan_seen_track_paths seen
          WHERE seen.relative_path=tracks.relative_path
        )
    """, (source,)).rowcount
    removed_books = connection.execute("""
      DELETE FROM books
      WHERE source_id=?
        AND NOT EXISTS (SELECT 1 FROM tracks WHERE tracks.book_id=books.id)
    """, (source,)).rowcount
    removed_series = connection.execute("""
      DELETE FROM series_info
      WHERE source_id=?
        AND NOT EXISTS (
          SELECT 1 FROM books
          WHERE books.source_id=series_info.source_id
            AND books.author_folder=series_info.series_path
        )
    """, (source,)).rowcount
    connection.execute("""
      DELETE FROM scan_errors
      WHERE source_id=?
        AND NOT EXISTS (
          SELECT 1 FROM scan_seen_track_paths seen
          WHERE seen.relative_path=scan_errors.relative_path
        )
    """, (source,))
    return removed_tracks, removed_books, removed_series


def scan(
    root: Path,
    database: Path,
    force: bool = False,
    limit: int | None = None,
    progress_every: int = 25,
) -> dict[str, int]:
    root = root.resolve()
    connection = open_database(database)
    source = source_id(connection, root)
    totals = {
        "found": 0, "scanned": 0, "unchanged": 0, "errors": 0, "books": 0,
        "removed_tracks": 0, "removed_books": 0, "removed_series": 0,
    }
    books_seen: set[str] = set()
    seen_track_paths: set[str] = set()
    cover_cache: dict[Path, str | None] = {}
    description_cache: dict[Path, DescriptionOverride] = {}
    series_info_cache: dict[Path, SeriesInfo] = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        for path in audio_files(root):
            if limit is not None and totals["found"] >= limit:
                break
            totals["found"] += 1
            relative_file = path.relative_to(root)
            relative = str(relative_file)
            # Record a discovered path before tag reading so an unreadable
            # existing file does not cause its earlier database row to vanish.
            seen_track_paths.add(relative)
            try:
                # stat before tags lets repeat scans avoid opening unchanged audio.
                stat = path.stat()
                book_path = str(relative_file.parent)
                books_seen.add(book_path)
                totals["books"] = len(books_seen)
                # A top-level Author - Series folder is represented once in
                # series_info, even when it contains many book directories.
                if len(relative_file.parts) > 1:
                    series_directory = root / relative_file.parts[0]
                    if series_directory not in series_info_cache:
                        series_info_cache[series_directory] = read_series_info(series_directory, root)
                        store_series_info(
                            connection, source, relative_file.parts[0],
                            series_info_cache[series_directory],
                        )
                if path.parent not in cover_cache:
                    cover_cache[path.parent] = find_cover_image(path.parent, root)
                cover_image_path = cover_cache[path.parent]
                if path.parent not in description_cache:
                    description_cache[path.parent] = read_description_override(path.parent, root)
                description_override = description_cache[path.parent]
                pseudo = ScanResult(
                    path, relative, "", None, None, None,
                    description_override.source_path, description_override.size_bytes, description_override.modified_ns,
                    stat.st_size, stat.st_mtime_ns, None, None, None, {}, {},
                )
                if not force and unchanged(connection, source, pseudo):
                    totals["unchanged"] += 1
                    update_book_cover(connection, source, book_path, cover_image_path)
                    if progress_every and totals["found"] % progress_every == 0:
                        report_progress(totals)
                    continue
                result = scan_file(path, root, cover_image_path, description_override)
                store_result(connection, source, result, now)
                totals["scanned"] += 1
            except (OSError, RuntimeError) as exc:
                connection.execute("""INSERT INTO scan_errors(source_id, relative_path, message, scanned_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_id, relative_path) DO UPDATE SET message=excluded.message, scanned_at=excluded.scanned_at""", (source, relative, str(exc), now))
                totals["errors"] += 1
            if totals["found"] % 100 == 0:
                connection.commit()
            if progress_every and totals["found"] % progress_every == 0:
                report_progress(totals)
        if limit is None:
            (
                totals["removed_tracks"], totals["removed_books"], totals["removed_series"],
            ) = remove_missing_entries(connection, source, seen_track_paths)
        connection.execute("UPDATE catalog_sources SET last_scan_at=? WHERE id=?", (now, source))
        connection.commit()
        return totals
    finally:
        connection.close()


def export_xml(database: Path, destination: Path) -> int:
    connection = open_database(database)
    try:
        catalogue = ET.Element("audiobook_catalog", schema_version=str(SCHEMA_VERSION))
        rows = connection.execute("""
          SELECT s.root_path, b.book_path, b.author_folder, b.series_folder, b.cover_image_path, t.* FROM tracks t
          JOIN books b ON b.id=t.book_id JOIN catalog_sources s ON s.id=b.source_id
          ORDER BY b.author_folder, b.series_folder, b.book_path, t.relative_path
        """).fetchall()
        for row in rows:
            track = ET.SubElement(catalogue, "track", source_root=row["root_path"], path=row["relative_path"],
                                  book_path=row["book_path"], author_folder=row["author_folder"] or "", series_folder=row["series_folder"] or "")
            if row["cover_image_path"]:
                ET.SubElement(track, "cover_image_path").text = row["cover_image_path"]
            for name in ("duration_seconds", "bitrate", "codec", "size_bytes", "modified_ns", "scanned_at"):
                value = row[name]
                if value is not None:
                    ET.SubElement(track, name).text = str(value)
            fields = ET.SubElement(track, "metadata")
            for key, values in json.loads(row["values_json"]).items():
                for value in values:
                    ET.SubElement(fields, "field", name=key).text = value
            raw = ET.SubElement(track, "raw_tags")
            for key, values in json.loads(row["raw_tags_json"]).items():
                for value in values:
                    ET.SubElement(raw, "tag", name=key).text = value
        ET.indent(catalogue, space="  ")
        destination.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(catalogue).write(destination, encoding="utf-8", xml_declaration=True)
        return len(rows)
    finally:
        connection.close()


def export_books_xml(database: Path, destination: Path) -> int:
    """Write one XML element per book, using its first recorded track for tags."""
    connection = open_database(database)
    try:
        catalogue = ET.Element("audiobook_catalog", schema_version=str(SCHEMA_VERSION), granularity="book")
        rows = connection.execute("""
          WITH ranked_tracks AS (
            SELECT t.*,
              ROW_NUMBER() OVER (PARTITION BY t.book_id ORDER BY t.id) AS track_order,
              COUNT(*) OVER (PARTITION BY t.book_id) AS track_count,
              SUM(COALESCE(t.duration_seconds, 0)) OVER (PARTITION BY t.book_id) AS total_duration_seconds,
              SUM(t.size_bytes) OVER (PARTITION BY t.book_id) AS total_size_bytes
            FROM tracks t
          )
          SELECT s.root_path, b.book_path, b.author_folder, b.series_folder, b.cover_image_path, r.*
          FROM ranked_tracks r
          JOIN books b ON b.id=r.book_id JOIN catalog_sources s ON s.id=b.source_id
          WHERE r.track_order=1
          ORDER BY b.author_folder, b.series_folder, b.book_path
        """).fetchall()
        for row in rows:
            book = ET.SubElement(
                catalogue, "book", source_root=row["root_path"], book_path=row["book_path"],
                author_folder=row["author_folder"] or "", series_folder=row["series_folder"] or "",
            )
            if row["cover_image_path"]:
                ET.SubElement(book, "cover_image_path").text = row["cover_image_path"]
            ET.SubElement(book, "track_count").text = str(row["track_count"])
            ET.SubElement(book, "duration_seconds").text = str(row["total_duration_seconds"])
            ET.SubElement(book, "size_bytes").text = str(row["total_size_bytes"])
            ET.SubElement(book, "representative_track_path").text = row["relative_path"]
            ET.SubElement(book, "representative_scanned_at").text = row["scanned_at"]
            fields = ET.SubElement(book, "metadata")
            for key, values in json.loads(row["values_json"]).items():
                for value in values:
                    ET.SubElement(fields, "field", name=key).text = value
            raw = ET.SubElement(book, "raw_tags")
            for key, values in json.loads(row["raw_tags_json"]).items():
                for value in values:
                    ET.SubElement(raw, "tag", name=key).text = value
        ET.indent(catalogue, space="  ")
        destination.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(catalogue).write(destination, encoding="utf-8", xml_declaration=True)
        return len(rows)
    finally:
        connection.close()


def export_book_list(database: Path, destination: Path) -> int:
    """Write a concise, alphabetized folder-based list of books."""
    connection = open_database(database)
    try:
        rows = connection.execute("""
          SELECT author_folder, book_path FROM books
          ORDER BY author_folder COLLATE NOCASE, book_path COLLATE NOCASE
        """).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            heading = row["author_folder"] or "Uncategorized"
            # A book is represented by the folder that directly contains tracks.
            book_name = Path(row["book_path"]).name or row["book_path"]
            grouped.setdefault(heading, []).append(book_name)

        lines: list[str] = []
        for heading in sorted(grouped, key=str.casefold):
            lines.append(heading)
            for book_name in sorted(grouped[heading], key=str.casefold):
                lines.append(f"     {book_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return len(rows)
    finally:
        connection.close()


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unknown"
    return f"{size_bytes / (1024 * 1024):,.2f} MB"


def metadata_text(values: dict[str, list[str]], key: str) -> str:
    return "; ".join(values.get(key, []))


def html_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def relative_cover_url(
    destination: Path,
    source_root: str,
    cover_image_path: str,
    source_root_override: Path | None = None,
) -> str:
    """Return a URL relative to the generated catalogue, not an absolute file URL."""
    stored_root = Path(source_root)
    if source_root_override:
        # An override may name this root itself (single-library export) or its
        # parent, allowing one mapped path to serve Fiction, Nonfiction, etc.
        cover_root = source_root_override
        if cover_root.name.casefold() != stored_root.name.casefold():
            cover_root = cover_root / stored_root.name
    else:
        cover_root = stored_root
    cover_path = cover_root / cover_image_path
    try:
        relative_path = os.path.relpath(cover_path, start=destination.parent)
    except ValueError as exc:
        raise RuntimeError(
            "The stored source root and destination are on different Windows mounts. "
            "Pass --source-root with the current mapped path (for example W:\\)."
        ) from exc
    return quote(Path(relative_path).as_posix(), safe="/")


class DescriptionHtmlSanitizer(HTMLParser):
    """Preserve harmless description formatting while dropping attributes/scripts."""

    allowed_tags = {
        "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
        "h4", "h5", "h6", "i", "li", "ol", "p", "pre", "span", "strong", "u", "ul",
    }
    void_tags = {"br"}
    blocked_tags = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_depth = 0

    def close_open_tags_through(self, tag: str) -> None:
        """Close nested allowed tags through *tag*, repairing malformed markup."""
        if tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.output.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.blocked_tags:
            self.blocked_depth += 1
            return
        if self.blocked_depth:
            return
        if tag in self.allowed_tags:
            # These tags cannot validly nest inside themselves in the markup we emit.
            if tag in {"li", "p"}:
                self.close_open_tags_through(tag)
            self.output.append(f"<{tag}>")
            if tag not in self.void_tags:
                self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.blocked_depth and tag.casefold() == "br":
            self.output.append("<br>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.blocked_tags:
            self.blocked_depth = max(0, self.blocked_depth - 1)
            return
        if self.blocked_depth:
            return
        if tag in self.allowed_tags and tag not in self.void_tags:
            self.close_open_tags_through(tag)

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"&#{name};")

    def finish(self) -> None:
        """Close tags left open by a truncated ID3 comment."""
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")


def sanitize_description_html(text: str) -> str:
    sanitizer = DescriptionHtmlSanitizer()
    sanitizer.feed(text)
    sanitizer.close()
    sanitizer.finish()
    return "".join(sanitizer.output)


def export_html(database: Path, destination: Path, source_root_override: Path | None = None) -> int:
    """Write an interactive, local HTML catalogue organized by top-level series folder."""
    connection = open_database(database)
    try:
        books = connection.execute("""
          WITH ranked_tracks AS (
            SELECT t.*,
              ROW_NUMBER() OVER (PARTITION BY t.book_id ORDER BY t.id) AS track_order,
              COUNT(*) OVER (PARTITION BY t.book_id) AS track_count,
              SUM(COALESCE(t.duration_seconds, 0)) OVER (PARTITION BY t.book_id) AS total_duration_seconds,
              SUM(t.size_bytes) OVER (PARTITION BY t.book_id) AS total_size_bytes
            FROM tracks t
          )
          SELECT b.id AS book_id, s.root_path, b.book_path, b.author_folder, b.series_folder,
                 b.cover_image_path, si.info_path AS series_info_path,
                 si.info_text AS series_info_text, si.is_complete AS series_is_complete,
                 r.relative_path, r.codec, r.values_json, r.track_count,
                 r.total_duration_seconds, r.total_size_bytes
          FROM ranked_tracks r
          JOIN books b ON b.id=r.book_id JOIN catalog_sources s ON s.id=b.source_id
          LEFT JOIN series_info si ON si.source_id=b.source_id AND si.series_path=b.author_folder
          WHERE r.track_order=1
          ORDER BY b.author_folder COLLATE NOCASE, b.book_path COLLATE NOCASE
        """).fetchall()
        track_rows = connection.execute("""
          SELECT book_id, relative_path, duration_seconds, size_bytes, values_json
          FROM tracks ORDER BY book_id, relative_path COLLATE NOCASE
        """).fetchall()
    finally:
        connection.close()

    tracks_by_book: dict[int, list[sqlite3.Row]] = {}
    for track in track_rows:
        tracks_by_book.setdefault(track["book_id"], []).append(track)
    series: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for book in books:
        library_name = Path(book["root_path"]).name or "Uncategorized"
        series_heading = book["author_folder"] or "Uncategorized"
        series.setdefault((library_name, series_heading), []).append(book)

    document = ["""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audiobook Catalog</title>
<style>
:root { color-scheme: light dark; font-family: Arial, Helvetica, sans-serif; font-size: 14px; line-height: 1.25; }
body { margin: 0 auto; max-width: 1060px; padding: .75rem; background: #f7f7f7; color: #202124; }
h1 { margin: .2rem 0 .8rem; font-size: 1.5rem; } h3 { margin: 0 0 .5rem; font-size: 1.1rem; } .library { margin: 1.25rem 0; } .series { margin: .8rem 0; }
.library > h2 { margin: 0; padding: .45rem .65rem; font-size: 1.3rem; background: #cfe6ff; color: #17212b; border-radius: .25rem; }
.series-heading { display: flex; align-items: center; min-height: 1.6rem; padding: .4rem .6rem; font-size: 1.15rem; background: #99ffcc; color: #17221d; border-radius: .25rem; }
.series-heading h3 { margin: 0; font-size: inherit; }.series-link { color: inherit; cursor: pointer; text-decoration: underline; }.series-complete-badge { display: block; height: 1.6rem; width: auto; max-width: 2.15rem; margin-left: auto; object-fit: contain; }
.covers { display: grid; grid-template-columns: repeat(5, minmax(0, 200px)); justify-content: start; gap: .55rem; padding: .65rem 0; }
.book-card { min-width: 0; text-align: center; } .cover-button { border: 0; background: transparent; cursor: pointer; padding: 0; width: 100%; }
.cover, .no-cover { box-sizing: border-box; width: 100%; aspect-ratio: 3 / 4; object-fit: cover; display: block; border-radius: .3rem; box-shadow: 0 1px 4px #0006; }
.no-cover { display: grid; place-items: center; padding: .6rem; background: #5d6a75; color: white; }
.book-name { display: block; margin-top: .25rem; font-weight: 700; font-size: .85rem; overflow-wrap: anywhere; }
.book-subtitle { display: block; color: #555; font-size: .75rem; overflow-wrap: anywhere; }
.book-detail { position: relative; margin: 0 0 .9rem; padding: .7rem; border: 1px solid #b9c0c5; border-radius: .25rem; background: white; }
.close-button { position: absolute; top: .35rem; right: .35rem; border: 1px solid #899; border-radius: .2rem; background: #f1f3f4; color: #202124; cursor: pointer; font-size: 1.25rem; line-height: 1; padding: .05rem .35rem; }
.detail-grid { display: grid; grid-template-columns: minmax(160px, 260px) 1fr; gap: .7rem; }.detail-cover { width: 100%; max-height: 420px; object-fit: contain; }
.metadata { border-collapse: collapse; width: 100%; max-width: 600px; font-size: .9rem; }.metadata th, .metadata td { padding: .25rem .4rem; border: 1px solid #9aa; text-align: left; vertical-align: top; }.metadata th { width: 9rem; background: #d8edf8; }
.description { white-space: normal; }.description p { margin: .35rem 0; }.series-detail { position: relative; margin: .55rem 0 0; padding: .7rem; border: 1px solid #b9c0c5; border-radius: .25rem; background: white; }.series-description { white-space: pre-wrap; }.series-description p { margin: .35rem 0; }.tracks { margin-top: .7rem; }.tracks li { margin: .2rem 0; }
@media (max-width: 850px) { .covers { grid-template-columns: repeat(3, minmax(0, 200px)); } .detail-grid { grid-template-columns: 1fr; } .detail-cover { max-width: 260px; } }
@media (max-width: 480px) { .covers { grid-template-columns: repeat(2, minmax(0, 1fr)); } body { padding: .5rem; } }
</style>
<script>
function toggleDetails(id) {
  const detail = document.getElementById(id);
  detail.hidden = !detail.hidden;
  if (!detail.hidden) detail.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
function closeDetails(id) {
  document.getElementById(id).hidden = true;
}
</script>
</head>
<body>
<h1>Audiobook Catalog</h1>
"""]
    current_library: str | None = None
    for series_index, (library_name, heading) in enumerate(
        sorted(series, key=lambda item: (item[0].casefold(), item[1].casefold()))
    ):
        if library_name != current_library:
            if current_library is not None:
                document.append('</section>')
            document.append(f'<section class="library"><h2>{html_escape(library_name)}</h2>')
            current_library = library_name
        books_in_series = series[(library_name, heading)]
        series_record = books_in_series[0]
        series_info_path = series_record["series_info_path"]
        series_detail_id = f"series-{series_index}"
        if series_info_path:
            heading_html = (
                f'<a class="series-link" href="#{series_detail_id}" '
                f'onclick="toggleDetails(\'{series_detail_id}\'); return false;" '
                f'aria-controls="{series_detail_id}">{html_escape(heading)}</a>'
            )
        else:
            heading_html = html_escape(heading)
        badge_html = (
            '<img class="series-complete-badge" src="zz_catalog_files/series_complete_small.png" '
            'alt="Series complete">'
            if series_record["series_is_complete"] else ''
        )
        document.append(
            f'<section class="series"><div class="series-heading"><h3>{heading_html}</h3>{badge_html}</div>'
        )
        if series_info_path:
            series_text = series_record["series_info_text"] or ''
            series_html = sanitize_description_html(series_text) if series_text else 'No series information available.'
            document.append(
                f'<article class="series-detail" id="{series_detail_id}" hidden><button class="close-button" type="button" '
                f'onclick="closeDetails(\'{series_detail_id}\')" aria-label="Close series details">&times;</button>'
                f'<div class="series-description">{series_html}</div></article>'
            )
        document.append('<div class="covers">')
        for book in sorted(books_in_series, key=lambda row: Path(row["book_path"]).name.casefold()):
            values = json.loads(book["values_json"])
            title = Path(book["book_path"]).name or book["book_path"]
            subtitle = metadata_text(values, "series")
            if metadata_text(values, "series_part"):
                subtitle = f"{subtitle}, Book {metadata_text(values, 'series_part')}" if subtitle else f"Book {metadata_text(values, 'series_part')}"
            detail_id = f"book-{book['book_id']}"
            if book["cover_image_path"]:
                image_url = relative_cover_url(
                    destination, book["root_path"], book["cover_image_path"], source_root_override,
                )
                cover = f'<img class="cover" src="{html_escape(image_url)}" alt="Cover: {html_escape(title)}" loading="lazy">'
            else:
                cover = '<span class="no-cover">No cover image</span>'
            document.append(
                f'<article class="book-card"><button class="cover-button" type="button" onclick="toggleDetails(\'{detail_id}\')" '
                f'aria-controls="{detail_id}">{cover}</button><span class="book-name">{html_escape(title)}</span>'
                f'<span class="book-subtitle">{html_escape(subtitle)}</span></article>'
            )
        document.append('</div>')
        for book in sorted(books_in_series, key=lambda row: Path(row["book_path"]).name.casefold()):
            values = json.loads(book["values_json"])
            title = Path(book["book_path"]).name or book["book_path"]
            description = metadata_text(values, "description")
            detail_id = f"book-{book['book_id']}"
            cover_html = ''
            if book["cover_image_path"]:
                image_url = relative_cover_url(
                    destination, book["root_path"], book["cover_image_path"], source_root_override,
                )
                cover_html = f'<img class="detail-cover" src="{html_escape(image_url)}" alt="Cover: {html_escape(title)}">'
            fields = [
                ("Author", metadata_text(values, "artist") or metadata_text(values, "album_artist")),
                ("Narrator", metadata_text(values, "narrator")),
                ("Genre", metadata_text(values, "genre")),
                ("Year", metadata_text(values, "year")),
                ("Series", metadata_text(values, "series")),
                ("Tracks", book["track_count"]),
                ("Total length", format_duration(book["total_duration_seconds"])),
                ("Total size", format_size(book["total_size_bytes"])),
                ("Codec", book["codec"] or ""),
            ]
            table = ''.join(f'<tr><th>{html_escape(label)}</th><td>{html_escape(value)}</td></tr>' for label, value in fields if value)
            track_items = []
            for track in tracks_by_book.get(book["book_id"], []):
                track_values = json.loads(track["values_json"])
                track_title = metadata_text(track_values, "title") or Path(track["relative_path"]).name
                track_items.append(
                    f'<li>{html_escape(track_title)} — {format_duration(track["duration_seconds"])}; '
                    f'{format_size(track["size_bytes"])}</li>'
                )
            track_list = ''.join(track_items)
            description_html = sanitize_description_html(description) if description else 'No description available.'
            document.append(
                f'<article class="book-detail" id="{detail_id}" hidden><button class="close-button" type="button" '
                f'onclick="closeDetails(\'{detail_id}\')" aria-label="Close details">&times;</button><div class="detail-grid"><div>{cover_html}</div><div>'
                f'<h3>{html_escape(title)}</h3><div class="description">{description_html}</div><table class="metadata">{table}</table>'
                f'</div></div><details class="tracks"><summary>Tracks ({book["track_count"]})</summary><ol>{track_list}</ol></details></article>'
            )
        document.append('</section>')
    if current_library is not None:
        document.append('</section>')
    document.append('</body></html>\n')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('\n'.join(document), encoding='utf-8')
    return len(books)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    scan_parser = subcommands.add_parser("scan", help="scan one audiobook root incrementally")
    scan_parser.add_argument("root", type=Path, help="audiobook root to scan")
    scan_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                             help=f"local SQLite catalogue (default: {DEFAULT_DATABASE})")
    scan_parser.add_argument("--force", action="store_true", help="re-read all audio files")
    scan_parser.add_argument("--limit", type=int, help="scan at most this many files (for a trial run)")
    scan_parser.add_argument("--progress-every", type=int, default=25,
                             help="print progress after this many tracks; use 0 to disable (default: 25)")
    xml_parser = subcommands.add_parser("export-xml", help="write a portable XML snapshot")
    xml_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                            help=f"local SQLite catalogue (default: {DEFAULT_DATABASE})")
    xml_parser.add_argument("destination", type=Path)
    books_xml_parser = subcommands.add_parser("export-books-xml", help="write one XML entry per book")
    books_xml_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                                  help=f"local SQLite catalogue (default: {DEFAULT_DATABASE})")
    books_xml_parser.add_argument("destination", type=Path)
    book_list_parser = subcommands.add_parser("export-book-list", help="write an alphabetized text list of book folders")
    book_list_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                                  help=f"local SQLite catalogue (default: {DEFAULT_DATABASE})")
    book_list_parser.add_argument("destination", type=Path)
    html_parser = subcommands.add_parser("export-html", help="write an interactive HTML catalogue")
    html_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                             help=f"local SQLite catalogue (default: {DEFAULT_DATABASE})")
    html_parser.add_argument("--source-root", type=Path,
                             help="current path corresponding to the source root stored in the database (for example W:\\)")
    html_parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "scan":
        if not args.root.is_dir():
            parser.error(f"not a directory: {args.root}")
        if args.progress_every < 0:
            parser.error("--progress-every must be zero or a positive number")
        totals = scan(args.root, args.database, args.force, args.limit, args.progress_every)
        print(
            "Finished — tracks: {found} found, {scanned} scanned, {unchanged} unchanged, {errors} errors; "
            "books: {books}; removed: {removed_tracks} tracks, {removed_books} empty books, {removed_series} series records."
            .format(**totals)
        )
    elif args.command == "export-xml":
        count = export_xml(args.database, args.destination)
        print(f"Exported {count} tracks to {args.destination}")
    elif args.command == "export-books-xml":
        count = export_books_xml(args.database, args.destination)
        print(f"Exported {count} books to {args.destination}")
    elif args.command == "export-book-list":
        count = export_book_list(args.database, args.destination)
        print(f"Exported {count} books to {args.destination}")
    else:
        count = export_html(args.database, args.destination, args.source_root)
        print(f"Exported {count} books to {args.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
