from __future__ import annotations

import gzip
import os
import time
from cinecalendar.db import Database
from cinecalendar.catalog import _refresh_if_stale, import_imdb_datasets, repair_rated_metadata_from_official_datasets
from cinecalendar.util import json_loads


def gzwrite(path, text):
    with gzip.open(path, 'wt', encoding='utf-8', newline='') as fh:
        fh.write(text)


def test_official_crew_and_names_enrich_directors(tmp_path):
    basics = tmp_path/'title.basics.tsv.gz'
    ratings = tmp_path/'title.ratings.tsv.gz'
    crew = tmp_path/'title.crew.tsv.gz'
    names = tmp_path/'name.basics.tsv.gz'
    gzwrite(basics,
        'tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres\n'
        'tt9900001\tmovie\tDirector Candidate\tDirector Candidate\t0\t2024\t\\N\t110\tCrime,Thriller\n')
    gzwrite(ratings,
        'tconst\taverageRating\tnumVotes\n'
        'tt9900001\t7.7\t5000\n')
    gzwrite(crew,
        'tconst\tdirectors\twriters\n'
        'tt9900001\tnm9900001\t\\N\n')
    gzwrite(names,
        'nconst\tprimaryName\tbirthYear\tdeathYear\tprimaryProfession\tknownForTitles\n'
        'nm9900001\tDirector Exact\t1970\t\\N\tdirector\ttt9900001\n')
    db = Database(tmp_path/'director.db')
    result = import_imdb_datasets(db, basics, ratings, 50, crew_gz=crew, names_gz=names)
    assert result['directors'] == 1
    with db.connect() as con:
        row = con.execute("select directors_json from movies where imdb_id='tt9900001'").fetchone()
    assert json_loads(row[0], []) == ['Director Exact']


def test_rated_metadata_repair_has_no_minimum_vote_threshold(tmp_path):
    basics = tmp_path/'title.basics.tsv.gz'
    crew = tmp_path/'title.crew.tsv.gz'
    names = tmp_path/'name.basics.tsv.gz'
    gzwrite(basics,
        'tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres\n'
        'tt9900002\tshort\tTitlu localizat\tOriginal Exact\t0\t2026\t\\N\t18\tDrama,Short\n')
    gzwrite(crew,
        'tconst\tdirectors\twriters\n'
        'tt9900002\tnm9900002\t\\N\n')
    gzwrite(names,
        'nconst\tprimaryName\tbirthYear\tdeathYear\tprimaryProfession\tknownForTitles\n'
        'nm9900002\tDirector Din IMDb\t1980\t\\N\tdirector\ttt9900002\n')

    db = Database(tmp_path/'rated-repair.db')
    now = '2026-09-20T00:00:00+00:00'
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,runtime_min,genres_json,directors_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                'tt9900002','local|local|2026|short','Titlu localizat','Titlu localizat',
                'titlu localizat','titlu localizat',2026,'short',None,'[]','[]','imdb_public_sync',now,now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,7,'2026-09-20','imdb_public_sync',?,?)""",
            (int(cur.lastrowid),now,now),
        )

    result = repair_rated_metadata_from_official_datasets(db, tmp_path)
    assert result['directors_filled'] == 1
    assert result['original_titles_corrected'] == 1
    assert result['genres_filled'] == 1
    assert result['runtime_filled'] == 1

    with db.connect() as con:
        row = con.execute(
            """SELECT original_title,runtime_min,genres_json,directors_json
               FROM movies WHERE imdb_id='tt9900002'"""
        ).fetchone()
        provenance = {
            r['field']: r['provider']
            for r in con.execute(
                "SELECT field,provider FROM metadata_provenance WHERE movie_id=(SELECT id FROM movies WHERE imdb_id='tt9900002')"
            ).fetchall()
        }
    assert row['original_title'] == 'Original Exact'
    assert int(row['runtime_min']) == 18
    assert json_loads(row['genres_json'], []) == ['Drama', 'Short']
    assert json_loads(row['directors_json'], []) == ['Director Din IMDb']
    assert provenance['directors'] == 'imdb_dataset'
    assert provenance['original_title'] == 'imdb_dataset'


def test_stale_official_metadata_cache_is_invalidated(tmp_path):
    path = tmp_path / "title.crew.tsv.gz"
    gzwrite(path, "tconst\tdirectors\twriters\n")
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))
    messages = []

    assert _refresh_if_stale(path, 36, messages.append, "IMDb title.crew") is True
    assert not path.exists()
    assert any("versiunea IMDb actuală" in msg for msg in messages)


def test_recent_official_metadata_cache_is_reused(tmp_path):
    path = tmp_path / "title.crew.tsv.gz"
    gzwrite(path, "tconst\tdirectors\twriters\n")
    messages = []

    assert _refresh_if_stale(path, 36, messages.append, "IMDb title.crew") is False
    assert path.exists()
