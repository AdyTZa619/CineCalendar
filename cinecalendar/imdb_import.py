from __future__ import annotations
import csv, os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from .db import Database
from .util import identity_key, json_dumps, normalize_text, sha256_file, split_csvish, to_float, to_int, utcnow_iso

ALIASES = {
    "const": ["Const", "IMDb ID", "imdb_id", "tconst"],
    "your_rating": ["Your Rating", "YourRating", "rating"],
    "date_rated": ["Date Rated", "DateRated", "date_rated"],
    "title": ["Title", "Primary Title", "primaryTitle"],
    "original_title": ["Original Title", "OriginalTitle", "originalTitle"],
    "url": ["URL", "Url", "url"],
    "title_type": ["Title Type", "TitleType", "titleType", "type"],
    "imdb_rating": ["IMDb Rating", "IMDbRating", "averageRating"],
    "runtime": ["Runtime", "Runtime (mins)", "Runtime Minutes", "runtimeMinutes"],
    "year": ["Year", "Start Year", "startYear"],
    "genres": ["Genres", "genres"],
    "num_votes": ["Num Votes", "NumVotes", "numVotes"],
    "release_date": ["Release Date", "ReleaseDate", "release_date"],
    "directors": ["Directors", "directors"],
}
REQUIRED = {"const", "your_rating", "date_rated", "title", "title_type"}

@dataclass
class ImportResult:
    file: str
    total_rows: int = 0
    new_ratings: list[tuple[str,int]] = field(default_factory=list)
    changed_ratings: list[tuple[str,int,int]] = field(default_factory=list)
    unchanged: int = 0
    merged_manual: int = 0
    skipped_same_file: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new_ratings or self.changed_ratings or self.merged_manual)


def _resolve_headers(fieldnames: list[str] | None) -> dict[str,str]:
    if not fieldnames:
        raise ValueError("CSV-ul nu are antet.")
    lowered = {x.strip().lower(): x for x in fieldnames}
    resolved: dict[str,str] = {}
    for canonical, candidates in ALIASES.items():
        for c in candidates:
            if c.lower() in lowered:
                resolved[canonical] = lowered[c.lower()]
                break
    missing = [x for x in REQUIRED if x not in resolved]
    if missing:
        raise ValueError("CSV IMDb invalid. Lipsesc coloanele obligatorii: " + ", ".join(sorted(missing)))
    return resolved


def validate_imdb_csv(path: str | Path) -> tuple[dict[str,str], int]:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError("Fișierul CSV nu există sau este gol.")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = _resolve_headers(reader.fieldnames)
        count = 0
        for row in reader:
            if any((v or "").strip() for v in row.values()):
                count += 1
    if count == 0:
        raise ValueError("CSV-ul nu conține ratinguri.")
    return headers, count


def _fallback_movie(con, title: str, original: str, year: int | None, title_type: str):
    tn=normalize_text(title); on=normalize_text(original or title)
    return con.execute("""SELECT * FROM movies
        WHERE year IS ? AND LOWER(COALESCE(title_type,''))=LOWER(?)
          AND (title_norm IN (?,?) OR original_title_norm IN (?,?))
        ORDER BY CASE WHEN title_norm=? THEN 0 WHEN original_title_norm=? THEN 1 ELSE 2 END, id
        LIMIT 1""", (year,title_type,tn,on,tn,on,tn,tn)).fetchone()


def import_imdb_csv(db: Database, path: str | Path) -> ImportResult:
    path = Path(path)
    headers, expected_count = validate_imdb_csv(path)
    stat = path.stat()
    digest = sha256_file(path)
    result = ImportResult(file=str(path), total_rows=expected_count)

    with db.connect() as con:
        if con.execute("SELECT 1 FROM import_files WHERE sha256=?", (digest,)).fetchone():
            result.skipped_same_file = True
            return result

    now = utcnow_iso()
    with path.open("r", encoding="utf-8-sig", newline="") as fh, db.tx() as con:
        reader = csv.DictReader(fh)
        for line_no, row in enumerate(reader, start=2):
            try:
                get = lambda k: (row.get(headers[k], "") if k in headers else "")
                imdb_id = get("const").strip() or None
                title = get("title").strip()
                original = get("original_title").strip() if "original_title" in headers else title
                if not original:
                    original = title
                year = to_int(get("year")) if "year" in headers else None
                title_type = get("title_type").strip() or "Movie"
                rating = to_int(get("your_rating"))
                if not title or rating is None or not 1 <= rating <= 10:
                    raise ValueError("titlu sau rating invalid")
                ident = identity_key(title, original, year, title_type)
                movie = None
                if imdb_id:
                    movie = con.execute("SELECT * FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()
                if movie is None:
                    # Robust fallback; year+type keeps remakes separate. Try exact title+original first, then
                    # a title-only identity so a manual entry can reconcile even when IMDb later supplies
                    # a different Original Title.
                    movie = con.execute("SELECT * FROM movies WHERE identity_key=? ORDER BY id LIMIT 1", (ident,)).fetchone()
                    if movie is None:
                        movie = _fallback_movie(con,title,original,year,title_type)
                genres = split_csvish(get("genres")) if "genres" in headers else []
                directors = split_csvish(get("directors")) if "directors" in headers else []
                imdb_rating = to_float(get("imdb_rating")) if "imdb_rating" in headers else None
                runtime = to_int(get("runtime")) if "runtime" in headers else None
                num_votes = to_int(get("num_votes")) if "num_votes" in headers else None
                release_date = get("release_date").strip() if "release_date" in headers else None
                if movie is None:
                    cur = con.execute("""INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,year,title_type,runtime_min,
                          genres_json,directors_json,imdb_rating,num_votes,release_date,source,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (imdb_id, ident, title, original, normalize_text(title), normalize_text(original), year, title_type, runtime, json_dumps(genres), json_dumps(directors),
                         imdb_rating, num_votes, release_date or None, "imdb_csv", now, now))
                    movie_id = cur.lastrowid
                else:
                    movie_id = movie["id"]
                    # Attach an IMDb ID to a prior manual record when identity fallback matched.
                    merged_manual = not movie["imdb_id"] and imdb_id
                    if merged_manual:
                        result.merged_manual += 1
                    con.execute("""UPDATE movies SET imdb_id=COALESCE(imdb_id,?), identity_key=?, title=?, original_title=?, title_norm=?, original_title_norm=?, year=?,
                          title_type=?, runtime_min=COALESCE(?,runtime_min), genres_json=CASE WHEN ?!='[]' THEN ? ELSE genres_json END,
                          directors_json=CASE WHEN ?!='[]' THEN ? ELSE directors_json END, imdb_rating=COALESCE(?,imdb_rating),
                          num_votes=COALESCE(?,num_votes), release_date=COALESCE(?,release_date), updated_at=? WHERE id=?""",
                        (imdb_id, ident, title, original, normalize_text(title), normalize_text(original), year, title_type, runtime,
                         json_dumps(genres), json_dumps(genres), json_dumps(directors), json_dumps(directors), imdb_rating,
                         num_votes, release_date or None, now, movie_id))
                old = con.execute("SELECT rating FROM ratings WHERE movie_id=?", (movie_id,)).fetchone()
                date_rated = get("date_rated").strip() or None
                if old is None:
                    con.execute("INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                                (movie_id, rating, date_rated, "imdb", now, now))
                    result.new_ratings.append((title, rating))
                elif old["rating"] != rating:
                    previous = int(old["rating"])
                    con.execute("UPDATE ratings SET rating=?, date_rated=?, source='imdb', imported_at=?, updated_at=? WHERE movie_id=?",
                                (rating, date_rated, now, now, movie_id))
                    result.changed_ratings.append((title, previous, rating))
                else:
                    # Still refresh source/date if a manual rating was reconciled.
                    con.execute("UPDATE ratings SET date_rated=COALESCE(?,date_rated), source='imdb', imported_at=?, updated_at=? WHERE movie_id=?",
                                (date_rated, now, now, movie_id))
                    result.unchanged += 1
            except Exception as exc:
                result.errors.append(f"Linia {line_no}: {exc}")
        if result.errors:
            raise ValueError("Import oprit; CSV invalid la unele linii: " + "; ".join(result.errors[:5]))
        con.execute("INSERT INTO import_files(path_name,size_bytes,mtime_ns,sha256,row_count,imported_at) VALUES(?,?,?,?,?,?)",
                    (path.name, stat.st_size, stat.st_mtime_ns, digest, expected_count, now))
    return result


def add_manual_rating(db: Database, title: str, year: int | None, rating: int, imdb_id: str | None = None,
                      genres: Iterable[str] | None = None, original_title: str | None = None, title_type: str = "Movie") -> int:
    if not title.strip():
        raise ValueError("Titlul este obligatoriu.")
    if not 1 <= int(rating) <= 10:
        raise ValueError("Ratingul trebuie să fie între 1 și 10.")
    title = title.strip(); original_title = (original_title or title).strip(); imdb_id = (imdb_id or "").strip() or None
    ident = identity_key(title, original_title, year, title_type)
    now = utcnow_iso()
    with db.tx() as con:
        movie = con.execute("SELECT * FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone() if imdb_id else None
        if movie is None:
            movie = con.execute("SELECT * FROM movies WHERE identity_key=? ORDER BY id LIMIT 1", (ident,)).fetchone()
        if movie is None:
            movie = _fallback_movie(con,title,original_title,year,title_type)
        if movie is None:
            cur = con.execute("""INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,year,title_type,genres_json,source,created_at,updated_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (imdb_id, ident, title, original_title, normalize_text(title), normalize_text(original_title), year, title_type, json_dumps(list(genres or [])), "manual", now, now))
            movie_id = cur.lastrowid
        else:
            movie_id = movie["id"]
            con.execute("UPDATE movies SET imdb_id=COALESCE(imdb_id,?), title=?, original_title=?, title_norm=?, original_title_norm=?, year=COALESCE(?,year), updated_at=? WHERE id=?",
                        (imdb_id, title, original_title, normalize_text(title), normalize_text(original_title), year, now, movie_id))
        con.execute("""INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,date('now'),'manual',?,?)
                     ON CONFLICT(movie_id) DO UPDATE SET rating=excluded.rating,date_rated=excluded.date_rated,source='manual',updated_at=excluded.updated_at""",
                    (movie_id, int(rating), now, now))
        return int(movie_id)
