"""
IMDb helper for Movies-HUB-bot.

Why this file exists
--------------------
`cinemagoer` (the library this bot used for the "IMDb" caption) changed
behaviour in April 2026:

  * IMDb put www.imdb.com behind a WAF, so the old web-page parser of
    `cinemagoer` no longer returns any data.
  * `cinemagoer` therefore dropped the web access system entirely and its
    default backend became the "datasets" one, which reads a local SQLite
    database (`cinemagoer.db`) that has to be built from IMDb's public
    (non-commercial) TSV dumps.

The old code did `imdb = Cinemagoer()` at import time, so on a fresh
deploy the bot crashed before it even started with:

    imdb._exceptions.IMDbDataAccessError: SQLite database does not exist
    or is not a file: 'cinemagoer.db'

This module keeps the bot working in both situations:

  * `CinemagoerBackend` - used when a Cinemagoer database is available
    (set the `IMDB_DB_URI` environment variable, or ship a `cinemagoer.db`
    file next to the bot).  This is the officially supported data source.
  * `WebBackend` - a small, dependency free fallback that talks to the same
    public JSON/GraphQL endpoints the IMDb website itself uses.  No
    database, no scraping of the HTML pages, works out of the box.

Both backends expose the two methods the bot needs:

    search_movie(title, results=10) -> list[SearchResult]
    get_movie(movie_id)              -> dict

The returned objects mimic the shape of the old `cinemagoer` objects, so
`utils.get_poster()` keeps working unchanged.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# IMDb title type id -> the "kind" string the bot has always used
KIND_MAP = {
    "movie": "movie",
    "tvSeries": "tv series",
    "tvMiniSeries": "tv mini-series",
    "tvMovie": "tv movie",
    "video": "video",
    "short": "short",
    "tvEpisode": "tv episode",
    "tvSpecial": "tv special",
    "videoGame": "video game",
}

MONTHS = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

def _credit_block(alias, category, first=5):
    """Build one aliased `credits` block of the GraphQL query."""
    return (
        '    {alias}: credits(first: {first}, filter: {{categories: ["{category}"]}}) {{\n'
        "      edges {{ node {{ name {{ nameText {{ text }} }} }} }}\n"
        "    }}\n"
    ).format(alias=alias, category=category, first=first)


def _build_title_query(movie_id):
    """The GraphQL query used to fetch the details of a title."""
    parts = [
        "query {\n",
        '  title(id: "', movie_id, '") {\n',
        "    id\n",
        "    titleText { text }\n",
        "    originalTitleText { text }\n",
        "    releaseYear { year }\n",
        "    releaseDate { day month year }\n",
        "    titleType { id text }\n",
        "    plots(first: 3) { edges { node { plotText { plainText } } } }\n",
        "    ratingsSummary { voteCount aggregateRating }\n",
        "    genres { genres { text } }\n",
        "    runtime { seconds }\n",
        "    countriesOfOrigin { countries { id text } }\n",
        "    spokenLanguages { spokenLanguages { id text } }\n",
        "    primaryImage { url width height }\n",
        "    akas(first: 8) { edges { node { text country { id } } } }\n",
        "    episodes { seasons { number } }\n",
        "    certificates(first: 5) { edges { node { id country { id } rating } } }\n",
        '    cast: credits(first: 15, filter: {categories: ["cast"]}) {\n',
        "      edges { node { name { nameText { text } } ... on Cast { characters { name } } } }\n",
        "    }\n",
    ]
    parts.append(_credit_block("directors", "director"))
    parts.append(_credit_block("writers", "writer"))
    parts.append(_credit_block("producers", "producer"))
    parts.append(_credit_block("composers", "composer"))
    parts.append(_credit_block("cinematographers", "cinematographer"))
    parts.append(_credit_block("musicDepartment", "music_department"))
    parts.append(
        "    companyCredits(first: 25) {\n"
        "      edges { node { company { companyText { text } } category { id text } } }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    return "".join(parts)


class SearchResult(dict):
    """A search hit.

    It is a plain dict (so ``result.get('year')`` keeps working) that also
    exposes the ``movieID`` attribute the bot uses, exactly like the
    objects returned by ``cinemagoer``.
    """

    @property
    def movieID(self):
        return self.get("movieID")


class IMDbBackendError(Exception):
    """Raised when a backend cannot answer a request."""


class WebBackend:
    """Uses IMDb's public suggestion + GraphQL endpoints (no database)."""

    SUGGESTION_URL = "https://v2.sg.media-imdb.com/suggestion/x/%s.json"
    GRAPHQL_URL = "https://api.graphql.imdb.com/"

    def __init__(self, timeout=15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def search_movie(self, title, results=10):
        title = (title or "").strip()
        if not title:
            return []
        url = self.SUGGESTION_URL % requests.utils.quote(title, safe="")
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as e:  # network / json / http errors
            raise IMDbBackendError(f"IMDb suggestion request failed: {e}") from e

        found = []
        for item in payload.get("d", []) or []:
            imdb_id = item.get("id") or ""
            if not imdb_id.startswith("tt"):
                continue
            kind_id = item.get("qid") or ""
            found.append(
                SearchResult(
                    movieID=imdb_id[2:],
                    imdbID=imdb_id[2:],
                    title=item.get("l"),
                    year=item.get("y"),
                    kind=KIND_MAP.get(kind_id, (item.get("q") or "").lower() or "movie"),
                    kind_id=kind_id,
                    cast=item.get("s") or "",
                    cover_url=self._clean_cover_url((item.get("i") or {}).get("imageUrl")),
                    rating=None,
                )
            )
            if len(found) >= results:
                break
        return found

    def get_movie(self, movie_id):
        imdb_id = self._normalise_id(movie_id)
        query = _build_title_query(imdb_id)
        body = {"query": query}
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://www.imdb.com",
            "Referer": "https://www.imdb.com/",
            "Accept": "application/graphql+json, application/json",
        }
        try:
            response = self.session.post(
                self.GRAPHQL_URL, json=body, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            raise IMDbBackendError(f"IMDb GraphQL request failed: {e}") from e

        if payload.get("errors"):
            raise IMDbBackendError(
                "IMDb GraphQL error: %s" % payload["errors"][0].get("message", "unknown")
            )
        title = (payload.get("data") or {}).get("title")
        if not title:
            raise IMDbBackendError(f"IMDb returned no data for {imdb_id}")
        return self._build_movie(title, imdb_id)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalise_id(movie_id):
        movie_id = str(movie_id or "").strip()
        if movie_id.isdigit():
            return "tt%s" % movie_id
        if not movie_id.startswith("tt"):
            raise IMDbBackendError(f"Invalid IMDb id: {movie_id}")
        return movie_id

    @staticmethod
    def _clean_cover_url(url):
        """Return a poster URL that also survives the bot's `.jpg` fallback.

        The bot retries with ``url.replace('.jpg', '._V1_UX360.jpg')`` when
        Telegram refuses the first image, so the URL we hand over must not
        contain a ``._V1_.`` suffix.
        """
        if not url:
            return None
        url = url.split("?")[0]
        for marker in ("._V1_", "._V1_UX", "._SX", "._SY"):
            if marker in url:
                url = url.split(marker)[0]
                break
        if not url.lower().endswith(".jpg"):
            url = url + ".jpg"
        return url

    @staticmethod
    def _names(edges):
        names = []
        for edge in edges or []:
            node = edge.get("node") or {}
            name = ((node.get("name") or {}).get("nameText") or {}).get("text")
            if name:
                names.append(name)
        return names

    @staticmethod
    def _cast(edges):
        cast = []
        for edge in edges or []:
            node = edge.get("node") or {}
            name = ((node.get("name") or {}).get("nameText") or {}).get("text")
            if not name:
                continue
            characters = [c.get("name") for c in (node.get("characters") or []) if c.get("name")]
            if characters:
                name = "%s (%s)" % (name, ", ".join(characters))
            cast.append(name)
        return cast

    def _build_movie(self, title, imdb_id):
        ratings = title.get("ratingsSummary") or {}
        runtime = title.get("runtime") or {}
        release_date = title.get("releaseDate") or {}
        release_year = title.get("releaseYear") or {}

        akas = []
        for edge in (title.get("akas") or {}).get("edges") or []:
            text = (edge.get("node") or {}).get("text")
            if text and text not in akas:
                akas.append(text)

        plots = []
        for edge in (title.get("plots") or {}).get("edges") or []:
            text = ((edge.get("node") or {}).get("plotText") or {}).get("plainText")
            if text:
                plots.append(text)

        countries = [
            c.get("text")
            for c in (title.get("countriesOfOrigin") or {}).get("countries") or []
            if c.get("text")
        ]
        languages = [
            lang.get("text")
            for lang in (title.get("spokenLanguages") or {}).get("spokenLanguages") or []
            if lang.get("text")
        ]
        genres = [
            g.get("text") for g in (title.get("genres") or {}).get("genres") or [] if g.get("text")
        ]
        certificates = []
        for edge in (title.get("certificates") or {}).get("edges") or []:
            node = edge.get("node") or {}
            country = (node.get("country") or {}).get("id")
            rating = node.get("rating")
            if country and rating:
                certificates.append("%s:%s" % (country, rating))

        seasons = [
            s.get("number")
            for s in ((title.get("episodes") or {}).get("seasons") or [])
            if s.get("number")
        ]

        distributors = []
        for edge in (title.get("companyCredits") or {}).get("edges") or []:
            node = edge.get("node") or {}
            category = (node.get("category") or {}).get("id")
            company = ((node.get("company") or {}).get("companyText") or {}).get("text")
            if company and category in ("distribution", "distributor"):
                if company not in distributors:
                    distributors.append(company)

        minutes = None
        if runtime.get("seconds"):
            minutes = int(round(runtime["seconds"] / 60))

        air_date = None
        if release_date.get("year"):
            air_date = (
                "%s %s %s"
                % (
                    release_date.get("day") or "",
                    MONTHS[release_date.get("month") or 0],
                    release_date.get("year"),
                )
            ).strip()

        kind_id = (title.get("titleType") or {}).get("id") or "movie"
        original_title = (title.get("originalTitleText") or {}).get("text")
        main_title = (title.get("titleText") or {}).get("text")

        return {
            "title": main_title,
            "imdbID": imdb_id[2:],
            "kind": KIND_MAP.get(kind_id, kind_id),
            "year": release_year.get("year"),
            "original air date": air_date,
            "release date": air_date,
            "rating": str(ratings.get("aggregateRating") or ""),
            "votes": ratings.get("voteCount"),
            "genres": genres,
            "runtimes": [minutes] if minutes else [],
            "runtime": minutes,
            "countries": countries,
            "languages": languages,
            "certificates": certificates,
            "akas": akas,
            "localized title": original_title,
            "number of seasons": len(seasons) if seasons else None,
            "seasons": seasons,
            "cast": self._cast((title.get("cast") or {}).get("edges")),
            "director": self._names((title.get("directors") or {}).get("edges")),
            "writer": self._names((title.get("writers") or {}).get("edges")),
            "producer": self._names((title.get("producers") or {}).get("edges")),
            "composer": self._names((title.get("composers") or {}).get("edges")),
            "cinematographer": self._names((title.get("cinematographers") or {}).get("edges")),
            "music department": self._names((title.get("musicDepartment") or {}).get("edges")),
            "distributors": distributors,
            "plot": plots,
            "plot outline": plots,
            "full-size cover url": self._clean_cover_url(
                (title.get("primaryImage") or {}).get("url")
            ),
            "url": "https://www.imdb.com/title/%s" % imdb_id,
        }


class CinemagoerBackend:
    """Uses a local Cinemagoer database (IMDb's non-commercial datasets)."""

    def __init__(self, uri):
        from imdb import Cinemagoer  # imported lazily, it is an optional backend

        self._ia = Cinemagoer("s3", uri=uri)

    def search_movie(self, title, results=10):
        return self._ia.search_movie(title, results=results)

    def get_movie(self, movie_id):
        movie_id = str(movie_id).strip()
        if movie_id.startswith("tt"):
            movie_id = movie_id[2:]
        if movie_id.isdigit():
            # the dataset backend stores the numeric part of the id only
            movie_id = movie_id.zfill(7)
        movie = self._ia.get_movie(movie_id)
        try:
            movie.setdefault("imdbID", movie_id)
        except Exception:  # pragma: no cover - defensive, the object is a dict
            pass
        return movie


def get_imdb_client():
    """Return the best available IMDb backend.

    Order of preference:
      1. a Cinemagoer database (`IMDB_DB_URI` env var or `cinemagoer.db`)
      2. the web backend (always available, no database required)

    This function never raises, so importing the bot can not fail because
    of IMDb anymore.
    """
    uri = os.environ.get("IMDB_DB_URI", "").strip()
    if not uri:
        for candidate in (
            os.path.join(BASE_DIR, "cinemagoer.db"),
            os.path.join(os.getcwd(), "cinemagoer.db"),
        ):
            if os.path.isfile(candidate):
                uri = "sqlite:///%s" % candidate
                break

    if uri:
        try:
            backend = CinemagoerBackend(uri)
            logger.info("Using the Cinemagoer dataset backend (%s)", uri)
            return backend
        except Exception as e:
            logger.warning(
                "Cinemagoer database not usable (%s) - falling back to the web backend", e
            )

    logger.info("Using the IMDb web backend")
    return WebBackend()


if __name__ == "__main__":  # tiny self test:  python3 imdb_helper.py
    client = get_imdb_client()
    hits = client.search_movie("the matrix", results=3)
    for hit in hits:
        print("hit:", hit.get("movieID"), hit.get("title"), hit.get("year"), hit.get("kind"))
    if hits:
        movie = client.get_movie(hits[0].movieID)
        for key in (
            "title", "year", "kind", "rating", "votes", "genres", "runtimes",
            "countries", "languages", "certificates", "akas", "cast", "director",
            "number of seasons", "full-size cover url",
        ):
            print("  %-22s %s" % (key, movie.get(key)))
