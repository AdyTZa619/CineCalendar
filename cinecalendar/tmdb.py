from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from typing import Any
import requests
from .db import Database
from .models import Movie
from .semantic import extract_semantic
from .util import json_dumps, json_loads, utcnow_iso

API_BASE="https://api.themoviedb.org/3"
IMG_BASE="https://image.tmdb.org/t/p/w342"

class TmdbProvider:
    def __init__(self, db: Database, token: str):
        self.db=db; self.token=token.strip()
        if not self.token: raise ValueError("Lipsește TMDb API Read Access Token.")
        self.session=requests.Session(); self.session.headers.update({"Authorization":f"Bearer {self.token}","accept":"application/json"})

    def _get(self,path:str,params:dict|None=None,cache_hours:int=168)->dict:
        key=path+"?"+json.dumps(params or {},sort_keys=True)
        with self.db.connect() as con:
            row=con.execute("SELECT payload_json,expires_at FROM metadata_cache WHERE provider='tmdb' AND cache_key=?",(key,)).fetchone()
        if row and row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"])>datetime.now(timezone.utc): return json_loads(row["payload_json"],{})
            except ValueError: pass
        resp=self.session.get(API_BASE+path,params=params,timeout=20); resp.raise_for_status(); data=resp.json()
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
        if not results: return movie
        tmdb_id=int(results[0]["id"])
        details=self._get(f"/movie/{tmdb_id}",{"append_to_response":"credits,keywords,external_ids"})
        movie.original_title=details.get("original_title") or movie.original_title
        movie.overview=details.get("overview") or movie.overview
        movie.runtime_min=details.get("runtime") or movie.runtime_min
        movie.countries=[x.get("name","") for x in details.get("production_countries",[]) if x.get("name")]
        movie.genres=[x.get("name","") for x in details.get("genres",[]) if x.get("name")] or movie.genres
        credits=details.get("credits",{}).get("crew",[])
        movie.directors=[x.get("name","") for x in credits if x.get("job")=="Director" and x.get("name")]
        kws=details.get("keywords",{}).get("keywords",[]) or details.get("keywords",{}).get("results",[])
        movie.keywords=[x.get("name","") for x in kws if x.get("name")]
        poster=details.get("poster_path"); movie.poster_url=(IMG_BASE+poster) if poster else movie.poster_url
        movie.semantic=extract_semantic(movie)
        with self.db.tx() as con:
            con.execute("""UPDATE movies SET tmdb_id=?,original_title=?,overview=?,runtime_min=?,countries_json=?,genres_json=?,directors_json=?,keywords_json=?,poster_url=?,semantic_json=?,updated_at=? WHERE id=?""",
                        (tmdb_id,movie.original_title,movie.overview,movie.runtime_min,json_dumps(movie.countries),json_dumps(movie.genres),json_dumps(movie.directors),json_dumps(movie.keywords),movie.poster_url,json_dumps(movie.semantic),utcnow_iso(),movie.id))
        return movie



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


def enrich_library(db: Database, token: str, limit: int = 100, progress=None) -> dict[str, int]:
    """Enrich up to ``limit`` IMDb-linked titles using the user's TMDb token.

    Explicit ratings are prioritized because richer metadata improves the learned profile first;
    remaining candidates are ordered by IMDb vote count. Failures are isolated per title so one
    unavailable TMDb mapping does not abort the whole batch.
    """
    limit=max(1, min(int(limit), 5000))
    provider=TmdbProvider(db, token)
    with db.connect() as con:
        rows=con.execute("""
            SELECT m.*, CASE WHEN r.movie_id IS NULL THEN 0 ELSE 1 END AS is_rated
            FROM movies m
            LEFT JOIN ratings r ON r.movie_id=m.id
            WHERE m.imdb_id IS NOT NULL AND TRIM(m.imdb_id)!=''
              AND (m.overview IS NULL OR TRIM(m.overview)='' OR m.tmdb_id IS NULL
                   OR m.poster_url IS NULL OR TRIM(m.poster_url)='')
            ORDER BY is_rated DESC, COALESCE(m.num_votes,0) DESC, m.id ASC
            LIMIT ?
        """, (limit,)).fetchall()
    enriched=missing=failed=0
    for idx,row in enumerate(rows,1):
        movie=_row_to_movie(row)
        try:
            before=(movie.overview, movie.poster_url, tuple(movie.keywords), tuple(movie.countries), tuple(movie.directors))
            provider.enrich_by_imdb(movie)
            after=(movie.overview, movie.poster_url, tuple(movie.keywords), tuple(movie.countries), tuple(movie.directors))
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
