from __future__ import annotations

import gzip
from cinecalendar.db import Database
from cinecalendar.catalog import import_imdb_datasets
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
