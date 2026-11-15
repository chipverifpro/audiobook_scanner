# Audiobook scanner

This is the initial, read-only catalogue builder for the audiobook collection.
It scans tags once, records a local SQLite master catalogue, and can export the
same data to XML. It does **not** write tags or rename files.

## Setup

Run with Python 3.10 or newer. The project is local, so its virtual environment
can live alongside the source code:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install mutagen
```

## Full set of sample commands:

```sh
python scanner.py scan /Volumes/OtterCloud/Audiobooks/Fiction
python scanner.py scan /Volumes/OtterCloud/Audiobooks/Nonfiction

python scanner.py export-xml tests/audiobook-catalog.xml

python scanner.py export-books-xml tests/audiobook-books.xml

python scanner.py export-book-list tests/book_list.txt
python scanner.py export-html /Volumes/OtterCloud/Audiobooks/catalog.html
```

Finished — tracks: 11764 found, 11764 scanned, 0 unchanged, 0 errors; books: 1037.
Finished — tracks: 409 found, 409 scanned, 0 unchanged, 0 errors; books: 52.
Exported 12173 tracks to tests/audiobook-catalog.xml
Exported 1089 books to tests/audiobook-books.xml
Exported 1089 books to tests/book_list.txt
Exported 1089 books to /Volumes/OtterCloud/Audiobooks/catalog.html

## Later re-scans do not reopen already scanned files

The next identical scan only performs directory/file `stat` checks; audio files
whose size and modified time have not changed are not reopened. `--force` is
available for a complete re-read. Progress is printed every 25 tracks by
default; use `--progress-every 100` for fewer messages or `--progress-every 0`
to disable it.

## Data model

`data/catalog.sqlite3` holds one source root, book paths inferred from folders,
and tracks. Each book has an optional `cover_image_path`: a path relative to the
audiobook root for a direct image file in its folder. The scanner only lists
image files; it never extracts embedded ID3 artwork. It prefers filenames with
`cover`, then `folder` or `front`, falling back to the first image by name.
`author_folder` and `series_folder` preserve the existing first two levels of
this library's directory structure. Each track keeps both normalised catalogue
fields and the raw Mutagen tag map (as JSON), preventing rare ID3 frames from
being discarded. XML is a derived, portable snapshot.

Place an optional UTF-8 `description.html` or `description.txt` directly in a
book folder to replace the ID3-derived description for every track in that
folder. This supports long or corrected descriptions; an empty file
intentionally clears the ID3 comment. If both files exist, `description.html`
takes precedence. The scanner detects changes to the selected file during
incremental scans.

Place an optional UTF-8 `series.html` or `series.txt` directly in a top-level
`Author - Series` folder to record information for the series as a whole. Its
source-relative filename and contents are stored once in the `series_info`
database table.
If the first word of the selected file is `COMPLETE` (case-insensitive), its
`is_complete` flag is set to `1`; otherwise it is `0`. When both files exist,
`series.html` takes precedence. The HTML export displays a completion badge in
the series header and makes that header a link which opens the stored series
information, without reading the network drive again.

## Other functions supported

`export-xml` creates one entry per audio track. 
`export-books-xml` creates one entry per book folder: it totals duration, byte
size, and track count, and uses the first recorded track's metadata and raw
tags as the book-level set. Its `representative_track_path` identifies that
source track.

`export-book-list` creates the compact `book_list.txt` format: each top-level
Author - Series folder heading is followed by its book folder names indented by
five spaces. Both headings and book names are sorted alphabetically.

`export-html` creates an interactive catalogue grouped by Author - Series
folder. It uses up to five cover thumbnails per row; clicking a cover toggles
that book's details. Series headers with a `series.html` or `series.txt` file
open its stored content; completed series show the
`zz_catalog_files/series_complete_small.png` badge. Cover images use URLs relative to the generated HTML file,
so place the catalogue at the audiobook-library level (for example,
`/Volumes/OtterCloud/Audiobooks/catalog.html`) to produce `Fiction/...` image
links that also work through another mount or an HTTP server.

## Other options to modify command behavior
`--source-root` is used by the `export-html` command when creating web links for cover images.  Strip this part off the front of the file path, and you get a relative path from the web page.  Example, the option `--source-root /Volumes/OtterCloud/Audiobooks` turns IMG SRC="file:///Volumes/OtterCloud/Audiobooks/Fiction/Adams/Hitchhiker/Cover.jpg" into IMG SRC="Fiction/Adams/Hitchiker/Cover.jpg".  Note that the path needs to look like the path used by the current operating system (Windows would use something like `--source-root W:\Audiobooks`).

`--database` allows changing which database file to use.  This project is now local, so its default `data/` directory is the intended location for the database. Use `--database /a/local/path/catalog.sqlite3` only when you want a separate catalogue.


Script to create all four webpages and book lists.

# Librivox partial catalog (Mac)

```sh
# Librivox
rm data/catalog_librivox.sqlite3
python scanner.py scan /Volumes/OtterCloud/Audiobooks/LibriVox --database data/catalog_librivox.sqlite3
python scanner.py export-html /Volumes/OtterCloud/Audiobooks/librivox.html --database data/catalog_librivox.sqlite3
```
rm data/catalog_librivox.sqlite3
python scanner.py scan /Volumes/OtterCloud/Audiobooks/LibriVox --database data/catalog_librivox.sqlite3
python scanner.py export-html /Volumes/OtterCloud/Audiobooks/librivox.html --database data/catalog_librivox.sqlite3
python scanner.py export-book-list /Volumes/OtterCloud/Audiobooks/librivox_list.txt --database data/catalog_librivox.sqlite3

# NonFiction partial catalog (Mac)
rm data/catalog_nonfiction.sqlite3
python scanner.py scan /Volumes/OtterCloud/Audiobooks/Nonfiction --database data/catalog_nonfiction.sqlite3
python scanner.py export-html /Volumes/OtterCloud/Audiobooks/nonfiction.html --database data/catalog_nonfiction.sqlite3
python scanner.py export-book-list /Volumes/OtterCloud/Audiobooks/nonfiction_list.txt --database data/catalog_nonfiction.sqlite3

# Fiction partial catalog (Mac)
rm data/catalog_fiction.sqlite3
python scanner.py scan /Volumes/OtterCloud/Audiobooks/Fiction --database data/catalog_fiction.sqlite3
python scanner.py export-html /Volumes/OtterCloud/Audiobooks/fiction.html --database data/catalog_fiction.sqlite3
python scanner.py export-book-list /Volumes/OtterCloud/Audiobooks/fiction_list.txt --database data/catalog_fiction.sqlite3

# Full Catalog on Mac
rm data/catalog.sqlite3
python scanner.py scan /Volumes/OtterCloud/Audiobooks/Fiction
python scanner.py scan /Volumes/OtterCloud/Audiobooks/Nonfiction
python scanner.py scan /Volumes/OtterCloud/Audiobooks/LibriVox
python scanner.py export-html /Volumes/OtterCloud/Audiobooks/catalog.html --source-root /Volumes/OtterCloud/Audiobooks
python scanner.py export-book-list /Volumes/OtterCloud/Audiobooks/book_list.txt

# Full Catalog on Windows
del data\catalog.sqlite3
python scanner.py scan W:\Audiobooks\Fiction
python scanner.py scan W:\Audiobooks\Nonfiction
python scanner.py scan W:\Audiobooks\LibriVox
python scanner.py export-html W:\Audiobooks\catalog_win.html --source-root W:\Audiobooks
python scanner.py export-book-list W:\Audiobooks\book_list_win.txt

# Example outputs
<li>Example list file: <A href="output_examples/book_list.txt">book_list.txt</A>
<li>Example html file: <A href="output_examples/catalog.html">catalog.html</A>
<br>Note that the html example doesn't have the image files here.
