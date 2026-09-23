from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from typing import Any
import requests
from .db import Database
from .metadata_provenance import metadata_sources_for_movie, record_metadata_sources
from .models import Movie
from .semantic import extract_semantic
from .util import json_dumps, json_loads, utcnow_iso

API_BASE="https://api.themoviedb.org/3"
IMG_BASE="https://image.tmdb.org/t/p/w500"

class TmdbProvider:
    def __init__(self, db: Database, token: str, request_timeout=(5.0, 12.0)):
        self.db=db; self.token=token.strip()
        if not self.token: raise ValueError("Lipsește TMDb API Read Access Token.")
        self.request_timeout=request_timeout
        self.last_status="idle"; self.last_error=""
        self.session=requests.Session(); self.session.headers.update({"Authorization":f"Bearer {self.token}","accept":"application/json"})

    def _get(self,path:str,params:dict|None=None,cache_hours:int=168)->dict:
        key=path+"?"+json.dumps(params or {},sort_keys=True)
        with self.db.connect() as con:
            row=con.execute("SELECT payload_json,expires_at FROM metadata_cache WHERE provider='tmdb' AND cache_key=?",(key,)).fetchone()
        if row and row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"])>datetime.now(timezone.utc):
                    self.last_status="cache"
                    return json_loads(row["payload_json"],{})
            except ValueError: pass
        try:
            resp=self.session.get(API_BASE+path,params=params,timeout=self.request_timeout); resp.raise_for_status(); data=resp.json()
        except requests.RequestException as exc:
            self.last_status="error"; self.last_error=str(exc)
            raise
        self.last_status="success"; self.last_error=""
        expires=(datetime.now(timezone.utc)+timedelta(hours=cache_hours)).replace(microsecond=0).isoformat()
        with self.db.tx() as con:
            con.execute("""INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at) VALUES('tmdb',?,?,?,?)
                         ON CONFLICT(provider,cache_key) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
                        (key,json_dumps(data),utcnow_iso(),expires))
        return data

    def test_connection(self)->bool:
        self._get("/configuration",cache_hours=24)
        return True

    def enrich_by_imdb(self,movie:Movie)->Movie:
        if not movie.imdb_id: return movie
        found=self._get(f"/find/{movie.imdb_id}",{"external_source":"imdb_id"})
        results=found.get("movie_results") or []
        if not results:
            self.last_status="not_found"; self.last_error=""
            return movie
        tmdb_id=int(results[0]["id"])
        details=self._get(
            f"/movie/{tmdb_id}",
            {
                "append_to_response":"credits,keywords,external_ids,images",
                "include_image_language":"ro,en,null",
                "language":"ro-RO",
            },
        )
        changed_fields: list[str] = ["tmdb_id"]
        overview_provider = ""
        sources = metadata_sources_for_movie(self.db, int(movie.id)) if movie.id is not None else {}
        had_overview = bool(str(movie.overview or "").strip())
        original_title = str(details.get("original_title") or "").strip()
        if not movie.original_title and original_title:
            movie.original_title = original_title
            changed_fields.append("original_title")
        overview = str(details.get("overview") or "").strip()
        can_localize_existing = bool(movie.overview) and sources.get("overview") in {"tmdb", "tmdb-en"}
        if overview and (not movie.overview or can_localize_existing):
            movie.overview = overview
            changed_fields.append("overview")
            overview_provider = "tmdb-ro"
        elif not movie.overview:
            english = self._get(f"/movie/{tmdb_id}", {"language":"en-US"})
            overview = str(english.get("overview") or "").strip()
            if overview:
                movie.overview = overview
                changed_fields.append("overview")
                overview_provider = "tmdb-en"
        runtime = details.get("runtime")
        if not movie.runtime_min and runtime:
            movie.runtime_min = runtime
            changed_fields.append("runtime_min")
        countries=[x.get("name","") for x in details.get("production_countries",[]) if x.get("name")]
        if not movie.countries and countries:
            movie.countries=countries
            changed_fields.append("countries")
        genres=[x.get("name","") for x in details.get("genres",[]) if x.get("name")]
        if not movie.genres and genres:
            movie.genres=genres
            changed_fields.append("genres")
        credits=details.get("credits",{}).get("crew",[])
        directors=[x.get("name","") for x in credits if x.get("job")=="Director" and x.get("name")]
        if not movie.directors and directors:
            movie.directors=directors
            changed_fields.append("directors")
        kws=details.get("keywords",{}).get("keywords",[]) or details.get("keywords",{}).get("results",[])
        keywords=[x.get("name","") for x in kws if x.get("name")]
        if not movie.keywords and keywords:
            movie.keywords=keywords
            changed_fields.append("keywords")
        poster=self._best_poster(details)
        if not movie.poster_url and poster:
            movie.poster_url=IMG_BASE+poster
            changed_fields.append("poster_url")
        # Translation-only upgrades must not silently alter the validated ranking semantics.
        if (
            not had_overview
            or any(field != "overview" for field in changed_fields if field != "tmdb_id")
            or not movie.semantic
        ):
            movie.semantic=extract_semantic(movie)
        stamp=utcnow_iso()
        with self.db.tx() as con:
            con.execute("""UPDATE movies SET tmdb_id=?,original_title=?,overview=?,runtime_min=?,countries_json=?,genres_json=?,directors_json=?,keywords_json=?,poster_url=?,semantic_json=?,updated_at=? WHERE id=?""",
                        (tmdb_id,movie.original_title,movie.overview,movie.runtime_min,json_dumps(movie.countries),json_dumps(movie.genres),json_dumps(movie.directors),json_dumps(movie.keywords),movie.poster_url,json_dumps(movie.semantic),stamp,movie.id))
            if movie.id is not None:
                record_metadata_sources(
                    con,
                    int(movie.id),
                    [field for field in changed_fields if field != "overview"],
                    "tmdb",
                    updated_at=stamp,
                )
                if overview_provider:
                    record_metadata_sources(con, int(movie.id), ["overview"], overview_provider, updated_at=stamp)
        return movie

    @staticmethod
    def _best_poster(details: dict) -> str:
        """Prefer a useful Romanian/English poster, then TMDb's canonical poster."""
        posters=list((details.get("images") or {}).get("posters") or [])
        language_priority={"ro":3,"en":2,None:1,"":1}
        def quality(item):
            language=language_priority.get(item.get("iso_639_1"),0)
            votes=int(item.get("vote_count") or 0)
            average=float(item.get("vote_average") or 0.0)
            width=int(item.get("width") or 0)
            return (language, min(votes,100), average, width)
        posters=[item for item in posters if item.get("file_path")]
        if posters:
            return str(max(posters,key=quality)["file_path"])
        return str(details.get("poster_path") or "")



def _row_to_movie(row) -> Movie:
    return Movie(
        id=row["id"], imdb_id=row["imdb_id"], title=row["title"], original_title=row["original_title"] or "",
        year=row["year"], title_type=row["title_type"] or "Movie", runtime_min=row["runtime_min"],
        genres=json_loads(row["genres_json"], []), directors=json_loads(row["directors_json"], []),
        countries=json_loads(row["countries_json"], []), overview=row["overview"] or "",
        keywords=json_loads(row["keywords_json"], []), imdb_rating=row["imdb_rating"], num_votes=row["num_votes"],
        release_date=row["release_date"], poster_url=row["poster_url"], source=row["source"],
        semantic=json_loads(row["semantic_json"], {}) or {},
    )


def enrich_library(db: Database, token: str, limit: int = 100, progress=None, *, rated_only: bool = False) -> dict[str, int]:
    """Enrich up to ``limit`` IMDb-linked titles using the user's TMDb token.

    Explicit ratings are prioritized because richer metadata improves the learned profile first;
    remaining candidates are ordered by IMDb vote count. Failures are isolated per title so one
    unavailable TMDb mapping does not abort the whole batch.
    """
    limit=max(1, min(int(limit), 5000))
    provider=TmdbProvider(db, token)
    rated_clause = "AND r.movie_id IS NOT NULL" if rated_only else ""
    with db.connect() as con:
        rows=con.execute(f"""
            SELECT m.*, CASE WHEN r.movie_id IS NULL THEN 0 ELSE 1 END AS is_rated
            FROM movies m
            LEFT JOIN ratings r ON r.movie_id=m.id
            WHERE m.imdb_id IS NOT NULL AND TRIM(m.imdb_id)!=''
              {rated_clause}
              AND (
                   m.overview IS NULL OR TRIM(m.overview)=''
                   OR m.poster_url IS NULL OR TRIM(m.poster_url)=''
                   OR m.runtime_min IS NULL
                   OR TRIM(COALESCE(m.genres_json,'')) IN ('','[]')
                   OR TRIM(COALESCE(m.directors_json,'')) IN ('','[]')
                   OR TRIM(COALESCE(m.countries_json,'')) IN ('','[]')
              )
            ORDER BY is_rated DESC, COALESCE(m.num_votes,0) DESC, m.id ASC
            LIMIT ?
        """, (limit,)).fetchall()
    enriched=missing=failed=0
    for idx,row in enumerate(rows,1):
        movie=_row_to_movie(row)
        try:
            before=(movie.overview, movie.poster_url, movie.runtime_min, tuple(movie.genres), tuple(movie.keywords), tuple(movie.countries), tuple(movie.directors))
            provider.enrich_by_imdb(movie)
            after=(movie.overview, movie.poster_url, movie.runtime_min, tuple(movie.genres), tuple(movie.keywords), tuple(movie.countries), tuple(movie.directors))
            if after != before:
                enriched += 1
            else:
                missing += 1
        except requests.RequestException:
            failed += 1
        except (ValueError, KeyError, TypeError):
            failed += 1
        if progress:
            progress(f"TMDb: {idx}/{len(rows)} • îmbogățite {enriched} • fără rezultat {missing} • erori {failed}")
    return {"requested": len(rows), "enriched": enriched, "missing": missing, "failed": failed}
