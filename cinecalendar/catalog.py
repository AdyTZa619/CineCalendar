from __future__ import annotations
import csv, gzip, time
from pathlib import Path
from typing import Callable
import requests
from .db import Database
from .semantic import extract_semantic
from .models import Movie
from .util import identity_key, json_dumps, normalize_text, split_csvish, to_float, to_int, utcnow_iso


IMDB_DATASET_URLS = {
    "basics": "https://datasets.imdbws.com/title.basics.tsv.gz",
    "ratings": "https://datasets.imdbws.com/title.ratings.tsv.gz",
    "crew": "https://datasets.imdbws.com/title.crew.tsv.gz",
    "names": "https://datasets.imdbws.com/name.basics.tsv.gz",
}


def _valid_gzip_tsv(path: Path, required_columns: set[str]) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 20:
            return False
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            header = fh.readline().strip().split("\t")
        return required_columns.issubset(set(header))
    except Exception:
        return False


def _download_stream(url: str, dest: Path, progress: Callable[[str],None], label: str, force: bool=False) -> Path:
    """Download one official IMDb dataset with a resumable .part file and atomic rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        progress(f"{label}: folosesc copia locală existentă.")
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    existing = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "CineCalendar/2.0 personal-noncommercial"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    with requests.get(url, stream=True, timeout=(15, 180), headers=headers, allow_redirects=True) as r:
        r.raise_for_status()
        append = existing > 0 and r.status_code == 206
        if not append:
            existing = 0
        mode = "ab" if append else "wb"
        total = int(r.headers.get("Content-Length") or 0) + existing
        done = existing
        last_emit = 0.0
        with part.open(mode) as fh:
            for chunk in r.iter_content(chunk_size=1024*1024):
                if not chunk:
                    continue
                fh.write(chunk); done += len(chunk)
                now=time.monotonic()
                if now-last_emit >= .4:
                    if total:
                        progress(f"{label}: {done/1024/1024:.1f}/{total/1024/1024:.1f} MB ({done*100/total:.0f}%)")
                    else:
                        progress(f"{label}: {done/1024/1024:.1f} MB")
                    last_emit=now
    part.replace(dest)
    progress(f"{label}: descărcare completă ({dest.stat().st_size/1024/1024:.1f} MB).")
    return dest


def download_official_imdb_datasets(cache_dir: str|Path, progress: Callable[[str],None]|None=None, force: bool=False) -> tuple[Path,Path]:
    """Compatibility/basic download: official IMDb title basics + aggregate ratings."""
    progress=progress or (lambda _ : None)
    root=Path(cache_dir); root.mkdir(parents=True,exist_ok=True)
    basics=root/'title.basics.tsv.gz'; ratings=root/'title.ratings.tsv.gz'
    if force:
        for p in (basics,ratings,basics.with_suffix(basics.suffix+'.part'),ratings.with_suffix(ratings.suffix+'.part')):
            p.unlink(missing_ok=True)
    _download_stream(IMDB_DATASET_URLS['basics'],basics,progress,'IMDb title.basics',force=False)
    if not _valid_gzip_tsv(basics,{'tconst','titleType','primaryTitle','originalTitle','isAdult','startYear','runtimeMinutes','genres'}):
        basics.unlink(missing_ok=True); raise ValueError('Fișierul title.basics descărcat nu este un dataset IMDb valid.')
    _download_stream(IMDB_DATASET_URLS['ratings'],ratings,progress,'IMDb title.ratings',force=False)
    if not _valid_gzip_tsv(ratings,{'tconst','averageRating','numVotes'}):
        ratings.unlink(missing_ok=True); raise ValueError('Fișierul title.ratings descărcat nu este un dataset IMDb valid.')
    return basics,ratings


def download_official_imdb_recommender_datasets(cache_dir: str|Path, progress: Callable[[str],None]|None=None,
                                                  force: bool=False) -> tuple[Path,Path,Path,Path]:
    """Download the four official datasets used by the rating-first recommender.

    crew + names let CineCalendar learn and apply director preferences automatically,
    rather than requiring a TMDb token or manual metadata entry.
    """
    progress = progress or (lambda _ : None)
    root = Path(cache_dir); root.mkdir(parents=True, exist_ok=True)
    basics, ratings = download_official_imdb_datasets(root, progress, force)
    crew = root/'title.crew.tsv.gz'; names = root/'name.basics.tsv.gz'
    if force:
        for p in (crew,names,crew.with_suffix(crew.suffix+'.part'),names.with_suffix(names.suffix+'.part')):
            p.unlink(missing_ok=True)
    _download_stream(IMDB_DATASET_URLS['crew'], crew, progress, 'IMDb title.crew', force=False)
    if not _valid_gzip_tsv(crew, {'tconst','directors'}):
        crew.unlink(missing_ok=True); raise ValueError('Fișierul title.crew descărcat nu este valid.')
    _download_stream(IMDB_DATASET_URLS['names'], names, progress, 'IMDb name.basics', force=False)
    if not _valid_gzip_tsv(names, {'nconst','primaryName'}):
        names.unlink(missing_ok=True); raise ValueError('Fișierul name.basics descărcat nu este valid.')
    return basics, ratings, crew, names


def bootstrap_official_imdb_catalog(db: Database, cache_dir: str|Path, min_votes: int=50,
                                    progress: Callable[[str],None]|None=None, force_download: bool=False) -> dict:
    """One-call setup: download, validate and import an IMDb catalog rich enough for recommendations."""
    progress=progress or (lambda _ : None)
    basics,ratings,crew,names=download_official_imdb_recommender_datasets(cache_dir,progress,force_download)
    progress('Construiesc catalogul local de recomandări și leg regizorii…')
    result=import_imdb_datasets(db,basics,ratings,min_votes,progress,crew_gz=crew,names_gz=names)
    result.update({'basics_path':str(basics),'ratings_path':str(ratings),'crew_path':str(crew),'names_path':str(names)})
    return result


CATALOG_ALIASES = {
    "imdb_id": ["imdb_id","Const","tconst","IMDb ID"], "title":["title","Title","primaryTitle"],
    "original_title":["original_title","Original Title","originalTitle"], "year":["year","Year","startYear"],
    "title_type":["title_type","Title Type","titleType"], "runtime":["runtime","Runtime","Runtime (mins)","runtimeMinutes"],
    "genres":["genres","Genres"], "directors":["directors","Directors"], "countries":["countries","Countries"],
    "overview":["overview","Overview","plot","Plot"], "keywords":["keywords","Keywords"],
    "imdb_rating":["imdb_rating","IMDb Rating","averageRating"], "num_votes":["num_votes","Num Votes","numVotes"],
    "release_date":["release_date","Release Date"], "poster_url":["poster_url","Poster URL","poster"],
}


def _headers(fieldnames):
    low={x.strip().lower():x for x in fieldnames or []}; out={}
    for k,vals in CATALOG_ALIASES.items():
        for v in vals:
            if v.lower() in low: out[k]=low[v.lower()]; break
    if "title" not in out: raise ValueError("Catalogul trebuie să aibă o coloană Title/title.")
    return out


def import_catalog_csv(db: Database, path: str|Path) -> dict:
    path=Path(path); added=updated=0; now=utcnow_iso()
    with path.open("r",encoding="utf-8-sig",newline="") as fh, db.tx() as con:
        reader=csv.DictReader(fh); h=_headers(reader.fieldnames)
        for row in reader:
            g=lambda k:(row.get(h[k],"") if k in h else "")
            title=g("title").strip()
            if not title: continue
            imdb_id=g("imdb_id").strip() or None; original=g("original_title").strip() or title
            year=to_int(g("year")); typ=g("title_type").strip() or "Movie"; ident=identity_key(title,original,year,typ)
            movie=Movie(imdb_id=imdb_id,title=title,original_title=original,year=year,title_type=typ,runtime_min=to_int(g("runtime")),
                genres=split_csvish(g("genres")),directors=split_csvish(g("directors")),countries=split_csvish(g("countries")),
                overview=g("overview").strip(),keywords=split_csvish(g("keywords")),imdb_rating=to_float(g("imdb_rating")),
                num_votes=to_int(g("num_votes")),release_date=g("release_date").strip() or None,poster_url=g("poster_url").strip() or None,source="catalog_csv")
            movie.semantic=extract_semantic(movie)
            existing=con.execute("SELECT id FROM movies WHERE imdb_id=?",(imdb_id,)).fetchone() if imdb_id else None
            if not existing: existing=con.execute("SELECT id FROM movies WHERE identity_key=? ORDER BY id LIMIT 1",(ident,)).fetchone()
            if not existing:
                tn,on=normalize_text(title),normalize_text(original)
                existing=con.execute("""SELECT id FROM movies WHERE year IS ? AND LOWER(COALESCE(title_type,''))=LOWER(?)
                    AND (title_norm IN (?,?) OR original_title_norm IN (?,?)) ORDER BY id LIMIT 1""",(year,typ,tn,on,tn,on)).fetchone()
            vals=(imdb_id,ident,title,original,normalize_text(title),normalize_text(original),year,typ,movie.runtime_min,json_dumps(movie.genres),json_dumps(movie.directors),json_dumps(movie.countries),movie.overview,
                  json_dumps(movie.keywords),json_dumps(movie.semantic),movie.imdb_rating,movie.num_votes,movie.release_date,movie.poster_url,"catalog_csv",now)
            if existing:
                con.execute("""UPDATE movies SET imdb_id=COALESCE(imdb_id,?),identity_key=?,title=?,original_title=?,title_norm=?,original_title_norm=?,year=?,title_type=?,runtime_min=COALESCE(?,runtime_min),
                  genres_json=?,directors_json=?,countries_json=?,overview=?,keywords_json=?,semantic_json=?,imdb_rating=COALESCE(?,imdb_rating),num_votes=COALESCE(?,num_votes),
                  release_date=COALESCE(?,release_date),poster_url=COALESCE(?,poster_url),source=?,updated_at=? WHERE id=?""", vals+(existing["id"],)); updated+=1
            else:
                con.execute("""INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,year,title_type,runtime_min,genres_json,directors_json,countries_json,
                 overview,keywords_json,semantic_json,imdb_rating,num_votes,release_date,poster_url,source,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals[:-1]+(now,now)); added+=1
    return {"added":added,"updated":updated}


def _load_director_names_for_eligible(crew_gz: Path, names_gz: Path, eligible_ids: set[str],
                                      progress: Callable[[str],None]) -> dict[str,list[str]]:
    """Resolve only director names needed by eligible movies, keeping memory bounded."""
    progress("Indexez regizorii IMDb pentru filmele eligibile…")
    ids_by_title: dict[str,list[str]] = {}
    needed: set[str] = set()
    with gzip.open(crew_gz,"rt",encoding="utf-8",newline="") as fh:
        r=csv.DictReader(fh,delimiter="\t")
        for idx,row in enumerate(r,1):
            tid=row.get("tconst") or ""
            if tid not in eligible_ids: continue
            raw=row.get("directors") or ""
            if raw and raw != "\\N":
                ids=[x for x in raw.split(",") if x and x != "\\N"][:6]
                if ids:
                    ids_by_title[tid]=ids; needed.update(ids)
            if idx%2_000_000==0:
                progress(f"Crew scanat: {idx:,} • regizori necesari: {len(needed):,}")
    progress(f"Rezolv numele pentru {len(needed):,} regizori…")
    name_map: dict[str,str] = {}
    remaining=set(needed)
    with gzip.open(names_gz,"rt",encoding="utf-8",newline="") as fh:
        r=csv.DictReader(fh,delimiter="\t")
        for idx,row in enumerate(r,1):
            nid=row.get("nconst") or ""
            if nid in remaining:
                name=row.get("primaryName") or ""
                if name and name != "\\N": name_map[nid]=name
                remaining.discard(nid)
                if not remaining: break
            if idx%2_000_000==0:
                progress(f"Nume scanate: {idx:,} • rămase: {len(remaining):,}")
    result={}
    for tid,ids in ids_by_title.items():
        names=[name_map[x] for x in ids if x in name_map]
        if names: result[tid]=names
    progress(f"Regizori legați pentru {len(result):,} titluri eligibile.")
    return result


def import_imdb_datasets(db: Database, basics_gz: str|Path, ratings_gz: str|Path, min_votes: int=50,
                         progress: Callable[[str],None]|None=None, crew_gz: str|Path|None=None,
                         names_gz: str|Path|None=None) -> dict:
    """Import official IMDb datasets efficiently, optionally resolving directors."""
    basics_gz=Path(basics_gz); ratings_gz=Path(ratings_gz)
    if not basics_gz.exists() or not ratings_gz.exists():
        raise ValueError("Lipsesc fișierele IMDb dataset selectate.")
    if not _valid_gzip_tsv(basics_gz,{'tconst','titleType','primaryTitle','originalTitle','isAdult','startYear','runtimeMinutes','genres'}):
        raise ValueError("title.basics.tsv.gz nu este valid.")
    if not _valid_gzip_tsv(ratings_gz,{'tconst','averageRating','numVotes'}):
        raise ValueError("title.ratings.tsv.gz nu este valid.")
    progress=progress or (lambda _:None); now=utcnow_iso(); movie_count=0
    progress("Indexez ratingurile IMDb…")
    ratings_map: dict[str, tuple[float|None,int]]={}
    with gzip.open(ratings_gz,"rt",encoding="utf-8",newline="") as fh:
        r=csv.DictReader(fh,delimiter="\t")
        for idx,row in enumerate(r,1):
            votes=to_int(row.get("numVotes"),0) or 0
            if votes >= min_votes and row.get("tconst"):
                ratings_map[row["tconst"]]=(to_float(row.get("averageRating")),votes)
            if idx%500000==0:
                progress(f"Ratinguri scanate: {idx:,} • eligibile: {len(ratings_map):,}")

    directors_map: dict[str,list[str]] = {}
    if crew_gz and names_gz:
        crew=Path(crew_gz); names=Path(names_gz)
        if _valid_gzip_tsv(crew,{'tconst','directors'}) and _valid_gzip_tsv(names,{'nconst','primaryName'}):
            directors_map=_load_director_names_for_eligible(crew,names,set(ratings_map),progress)

    progress(f"Ratinguri eligibile indexate: {len(ratings_map):,}. Construiesc catalogul…")
    sql="""INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,year,title_type,runtime_min,genres_json,directors_json,semantic_json,
             imdb_rating,num_votes,source,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(imdb_id) DO UPDATE SET
               identity_key=excluded.identity_key,title=excluded.title,original_title=excluded.original_title,
               title_norm=excluded.title_norm,original_title_norm=excluded.original_title_norm,year=excluded.year,title_type=excluded.title_type,
               runtime_min=COALESCE(excluded.runtime_min,movies.runtime_min),genres_json=excluded.genres_json,
               directors_json=CASE WHEN excluded.directors_json!='[]' THEN excluded.directors_json ELSE movies.directors_json END,
               semantic_json=excluded.semantic_json,imdb_rating=excluded.imdb_rating,num_votes=excluded.num_votes,
               source='imdb_dataset',updated_at=excluded.updated_at"""
    batch=[]
    with db.tx() as con, gzip.open(basics_gz,"rt",encoding="utf-8",newline="") as fh:
        r=csv.DictReader(fh,delimiter="\t")
        for idx,row in enumerate(r,1):
            if row.get("isAdult") == "1" or row.get("titleType") not in {"movie","short","tvMovie","video"}:
                continue
            tconst=row.get("tconst") or ""; stage=ratings_map.get(tconst)
            if not stage: continue
            title=row.get("primaryTitle") or ""; original=row.get("originalTitle") or title
            if not title: continue
            year=to_int(row.get("startYear")); typ=row.get("titleType") or "movie"; ident=identity_key(title,original,year,typ)
            genres=[] if row.get("genres") in {None,"\\N"} else row.get("genres","").split(",")
            directors=directors_map.get(tconst,[])
            m=Movie(imdb_id=tconst,title=title,original_title=original,year=year,title_type=typ,runtime_min=to_int(row.get("runtimeMinutes")),
                    genres=genres,directors=directors,imdb_rating=stage[0],num_votes=stage[1],source="imdb_dataset")
            m.semantic=extract_semantic(m)
            batch.append((tconst,ident,title,original,normalize_text(title),normalize_text(original),year,typ,m.runtime_min,
                          json_dumps(genres),json_dumps(directors),json_dumps(m.semantic),m.imdb_rating,m.num_votes,"imdb_dataset",now,now))
            movie_count+=1
            if len(batch)>=5000:
                con.executemany(sql,batch);batch.clear()
            if movie_count%25000==0:
                progress(f"Catalog local: {movie_count:,} titluri…")
        if batch: con.executemany(sql,batch)
    progress(f"Catalog IMDb finalizat: {movie_count:,} titluri eligibile.")
    return {"ratings_stage":len(ratings_map),"movies":movie_count,"directors":len(directors_map),"min_votes":min_votes}
