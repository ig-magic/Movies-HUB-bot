#!/usr/bin/env python3
"""
Build a `cinemagoer.db` from IMDb's public (non-commercial) datasets.

Since April 2026 `cinemagoer` only works with the IMDb datasets that are
published on https://datasets.imdbws.com/ - it no longer parses the IMDb
web pages.  The bot therefore needs a local SQLite database
(`cinemagoer.db`) to be able to show the IMDb caption.

This script downloads the dataset files, trims them down to the title types
that make sense for a movie bot and imports everything into a SQLite
database with Cinemagoer's own importer.

    # full run (downloads ~1.8 GB, needs ~2 GB RAM and ~3 GB disk)
    python3 tools/build_imdb_db.py --out cinemagoer.db

    # reuse already downloaded files
    python3 tools/build_imdb_db.py --tsv-dir ~/imdb-datasets --out cinemagoer.db

    # smallest possible database (no cast, no foreign titles)
    python3 tools/build_imdb_db.py --out cinemagoer.db --skip-principals \
            --akas-region US

Notes
-----
* The resulting database is still large (roughly 0.5 - 1.5 GB depending on
  the options) and the import takes a while.  On a small Render free
  instance (0.5 GB disk) it will not fit - use the web backend instead
  (the default, see `imdb_helper.py`).
* The dataset can be downloaded manually from
  https://datasets.imdbws.com/ as well.
"""

import argparse
import gzip
import os
import shutil
import sys
import tempfile
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

DATASET_URL = "https://datasets.imdbws.com/"

DATASET_FILES = (
    "title.basics.tsv.gz",
    "title.ratings.tsv.gz",
    "title.akas.tsv.gz",
    "title.crew.tsv.gz",
    "title.principals.tsv.gz",
    "title.episode.tsv.gz",
    "name.basics.tsv.gz",
)

DEFAULT_KEEP_TYPES = "movie,tvSeries,tvMiniSeries,tvMovie"

USER_AGENT = "Mozilla/5.0 (compatible; Movies-HUB-bot/1.0)"


def log(message):
    print("[build_imdb_db] %s" % message, flush=True)


def download_file(name, target_dir):
    destination = os.path.join(target_dir, name)
    if os.path.isfile(destination) and os.path.getsize(destination) > 0:
        log("%s already downloaded" % name)
        return destination
    log("downloading %s" % name)
    request = urllib.request.Request(
        DATASET_URL + name, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with open(destination, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    return destination


def read_tsv_gz(path):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        for line in handle:
            yield header, line.rstrip("\n").split("\t")


def write_tsv_gz(path, header, rows):
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as handle:
        handle.write("\t".join(header) + "\n")
        for row in rows:
            handle.write("\t".join(row) + "\n")


def filter_title_basics(source, destination, keep_types):
    """Keep only the wanted title types, return the set of kept tconsts."""
    kept = set()
    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as src:
        header = src.readline().rstrip("\n").split("\t")
        type_index = header.index("titleType")
        const_index = header.index("tconst")
        with gzip.open(destination, "wt", encoding="utf-8", compresslevel=6) as dst:
            dst.write("\t".join(header) + "\n")
            for line in src:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(header):
                    continue
                if parts[type_index] in keep_types:
                    kept.add(parts[const_index])
                    dst.write(line)
    log("title.basics: kept %d titles" % len(kept))
    return kept


def filter_by_tconst(source, destination, kept, column=0, extra=None):
    """Keep only the rows whose `column` value is in `kept`."""
    count = 0
    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as src:
        header = src.readline().rstrip("\n").split("\t")
        with gzip.open(destination, "wt", encoding="utf-8", compresslevel=6) as dst:
            dst.write("\t".join(header) + "\n")
            for line in src:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(header):
                    continue
                if parts[column] in kept and (extra is None or extra(parts, header)):
                    count += 1
                    dst.write(line)
    log("%s: kept %d rows" % (os.path.basename(source), count))
    return count


def filter_principals(source, destination, kept, max_per_title):
    """Keep at most `max_per_title` rows per title, collect the name ids."""
    counts = {}
    names = set()
    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as src:
        header = src.readline().rstrip("\n").split("\t")
        const_index = header.index("tconst")
        name_index = header.index("nconst")
        with gzip.open(destination, "wt", encoding="utf-8", compresslevel=6) as dst:
            dst.write("\t".join(header) + "\n")
            for line in src:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(header):
                    continue
                const = parts[const_index]
                if const not in kept:
                    continue
                seen = counts.get(const, 0)
                if seen >= max_per_title:
                    continue
                counts[const] = seen + 1
                names.add(parts[name_index])
                dst.write(line)
    log("title.principals: kept %d rows, %d names" % (sum(counts.values()), len(names)))
    return names


def filter_crew(source, destination, kept):
    """Keep the crew rows of the kept titles, collect the name ids."""
    names = set()
    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as src:
        header = src.readline().rstrip("\n").split("\t")
        const_index = header.index("tconst")
        directors_index = header.index("directors")
        writers_index = header.index("writers")
        with gzip.open(destination, "wt", encoding="utf-8", compresslevel=6) as dst:
            dst.write("\t".join(header) + "\n")
            for line in src:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(header):
                    continue
                if parts[const_index] not in kept:
                    continue
                for value in (parts[directors_index], parts[writers_index]):
                    for name_id in (value or "").split(","):
                        if name_id and name_id != "\\N":
                            names.add(name_id)
                dst.write(line)
    log("title.crew: kept %d names" % len(names))
    return names


def filter_names(source, destination, kept_names):
    count = 0
    with gzip.open(source, "rt", encoding="utf-8", errors="replace") as src:
        header = src.readline().rstrip("\n").split("\t")
        const_index = header.index("nconst")
        with gzip.open(destination, "wt", encoding="utf-8", compresslevel=6) as dst:
            dst.write("\t".join(header) + "\n")
            for line in src:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(header):
                    continue
                if parts[const_index] in kept_names:
                    count += 1
                    dst.write(line)
    log("name.basics: kept %d names" % count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=os.path.join(BASE_DIR, "cinemagoer.db"),
        help="path of the SQLite database to create (default: ./cinemagoer.db)",
    )
    parser.add_argument(
        "--tsv-dir",
        default=None,
        help="directory with the downloaded *.tsv.gz files (default: download them)",
    )
    parser.add_argument(
        "--keep-types",
        default=DEFAULT_KEEP_TYPES,
        help="comma separated list of title types to keep (default: %s)" % DEFAULT_KEEP_TYPES,
    )
    parser.add_argument(
        "--max-principals",
        type=int,
        default=10,
        help="maximum number of cast/crew rows kept per title (default: 10)",
    )
    parser.add_argument(
        "--skip-principals",
        action="store_true",
        help="do not import the cast (much smaller database, no cast in captions)",
    )
    parser.add_argument(
        "--akas-region",
        default="US",
        help="only keep alternative titles of this region ('US' or '' for all, default: US)",
    )
    parser.add_argument(
        "--keep-downloads",
        action="store_true",
        help="do not delete the downloaded/trimmed TSV files when finished",
    )
    args = parser.parse_args()

    keep_types = {t.strip() for t in args.keep_types.split(",") if t.strip()}

    temp_dir = None
    if args.tsv_dir:
        source_dir = os.path.abspath(args.tsv_dir)
        if not os.path.isdir(source_dir):
            sys.exit("Directory not found: %s" % source_dir)
    else:
        temp_dir = tempfile.mkdtemp(prefix="imdb-datasets-")
        source_dir = temp_dir
        for name in DATASET_FILES:
            try:
                download_file(name, source_dir)
            except Exception as e:
                sys.exit("Download of %s failed: %s" % (name, e))

    build_dir = tempfile.mkdtemp(prefix="imdb-build-")
    try:
        log("trimming the datasets ...")
        kept = filter_title_basics(
            os.path.join(source_dir, "title.basics.tsv.gz"),
            os.path.join(build_dir, "title.basics.tsv.gz"),
            keep_types,
        )
        if not kept:
            sys.exit("No title kept - check --keep-types")

        for name in ("title.ratings.tsv.gz", "title.episode.tsv.gz"):
            path = os.path.join(source_dir, name)
            if os.path.isfile(path):
                filter_by_tconst(path, os.path.join(build_dir, name), kept)

        akas_path = os.path.join(source_dir, "title.akas.tsv.gz")
        if os.path.isfile(akas_path):
            region = args.akas_region

            def keep_aka(parts, header):
                if not region:
                    return True
                index = header.index("region")
                return parts[index] in (region, "")

            filter_by_tconst(
                akas_path, os.path.join(build_dir, "title.akas.tsv.gz"), kept, extra=keep_aka
            )

        names = set()
        crew_path = os.path.join(source_dir, "title.crew.tsv.gz")
        if os.path.isfile(crew_path):
            names |= filter_crew(
                crew_path, os.path.join(build_dir, "title.crew.tsv.gz"), kept
            )

        principals_path = os.path.join(source_dir, "title.principals.tsv.gz")
        if not args.skip_principals and os.path.isfile(principals_path):
            names |= filter_principals(
                principals_path,
                os.path.join(build_dir, "title.principals.tsv.gz"),
                kept,
                args.max_principals,
            )

        names_path = os.path.join(source_dir, "name.basics.tsv.gz")
        if os.path.isfile(names_path) and names:
            filter_names(names_path, os.path.join(build_dir, "name.basics.tsv.gz"), names)

        log("importing the datasets into %s (this takes a while) ..." % args.out)
        from imdb.parser.s3.importer import import_dir

        if os.path.exists(args.out):
            os.remove(args.out)
        import_dir(build_dir, "sqlite:///%s" % os.path.abspath(args.out))
        size = os.path.getsize(args.out) / (1024 * 1024)
        log("done: %s (%.0f MB)" % (args.out, size))
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
        if temp_dir and not args.keep_downloads:
            shutil.rmtree(temp_dir, ignore_errors=True)
        elif temp_dir:
            log("downloaded files kept in %s" % temp_dir)


if __name__ == "__main__":
    main()
