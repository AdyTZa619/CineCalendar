from __future__ import annotations

import csv
from datetime import date

from cinecalendar.db import Database
from cinecalendar.imdb_import import import_imdb_csv
from cinecalendar.catalog import import_catalog_csv
from cinecalendar.profile import build_profile
from cinecalendar.recommendation import RecommendationEngine, WEIGHTS


HEADERS = [
    'Const','Your Rating','Date Rated','Title','Original Title','URL','Title Type','IMDb Rating',
    'Runtime (mins)','Year','Genres','Num Votes','Release Date','Directors'
]


def dbtmp(tmp_path):
    return Database(tmp_path / 'v2.db')


def rated(i, rating, title, genre, director='Regizor Test'):
    return {
        'Const': i, 'Your Rating': str(rating), 'Date Rated': '2026-09-01',
        'Title': title, 'Original Title': title, 'URL': '', 'Title Type': 'Movie',
        'IMDb Rating': '7.0', 'Runtime (mins)': '105', 'Year': '2020',
        'Genres': genre, 'Num Votes': '10000', 'Release Date': '2020-01-01',
        'Directors': director,
    }


def write_ratings(path, rows):
    with open(path, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=HEADERS)
        w.writeheader(); w.writerows(rows)


def test_normal_mode_is_rating_first_not_calendar_first():
    personal = WEIGHTS['taste'] + WEIGHTS['semantic'] + WEIGHTS['director_cinema']
    context = WEIGHTS['calendar'] + WEIGHTS['season']
    assert abs(personal - .80) < 1e-12
    assert abs(context - .06) < 1e-12
    assert WEIGHTS['taste'] > context * 8


def test_profile_v2_contains_positive_elite_negative_vectors(tmp_path):
    db = dbtmp(tmp_path)
    p = tmp_path / 'ratings.csv'
    write_ratings(p, [
        rated('tt9100001', 10, 'Loved War', 'War,History', 'Director Loved'),
        rated('tt9100002', 9, 'Loved History', 'History', 'Director Loved'),
        rated('tt9100003', 3, 'Bad Comedy', 'Comedy', 'Director Bad'),
        rated('tt9100004', 4, 'Bad Comedy Two', 'Comedy', 'Director Bad'),
    ])
    import_imdb_csv(db, p)
    profile = build_profile(db)
    assert profile['version'] >= 2
    assert profile['global_mean_rating'] > 0
    assert profile['positive_vector']
    assert profile['elite_vector']
    assert profile['negative_vector']
    assert profile['features']['genre:history']['mean_rating'] >= 9.0
    assert profile['features']['genre:comedy']['mean_rating'] <= 4.0


def test_predicted_rating_favors_patterns_user_rates_highly(tmp_path):
    db = dbtmp(tmp_path)
    p = tmp_path / 'ratings.csv'
    rows = []
    for n, r in enumerate([10, 9, 9, 8], 1):
        rows.append(rated(f'tt92000{n:02d}', r, f'History {n}', 'History,War', 'Director Loved'))
    for n, r in enumerate([2, 3, 4, 4], 1):
        rows.append(rated(f'tt92100{n:02d}', r, f'Comedy {n}', 'Comedy', 'Director Bad'))
    write_ratings(p, rows)
    import_imdb_csv(db, p); build_profile(db)

    cat = tmp_path / 'catalog.csv'
    cat.write_text(
        'imdb_id,title,year,title_type,genres,directors,imdb_rating,num_votes,overview\n'
        'tt9290001,Great History,2024,Movie,"History,War",Director Loved,7.2,20000,war history battle\n'
        'tt9290002,Generic Comedy,2024,Movie,Comedy,Director Bad,8.2,200000,funny comedy\n',
        encoding='utf-8'
    )
    import_catalog_csv(db, cat)
    recs = RecommendationEngine(db).recommend(date(2026,9,14), 2, candidate_limit=1000)
    assert recs
    assert recs[0].movie.title == 'Great History'
    assert recs[0].score.predicted_rating > 6.5
    # A high-public-score title built from patterns repeatedly rated 2–4 should not
    # survive merely because IMDb likes it. High-confidence bad fits are suppressed.
    assert all(r.movie.title != 'Generic Comedy' for r in recs)


def test_decision_pick_returns_one_primary_and_backups_and_never_seen(tmp_path):
    db = dbtmp(tmp_path)
    p = tmp_path / 'ratings.csv'
    write_ratings(p, [rated('tt9300001', 9, 'Already Seen', 'Crime,Thriller', 'Director X')])
    import_imdb_csv(db, p); build_profile(db)
    cat = tmp_path / 'catalog.csv'
    cat.write_text(
        'imdb_id,title,year,title_type,genres,directors,imdb_rating,num_votes,overview\n'
        'tt9300001,Already Seen,2020,Movie,"Crime,Thriller",Director X,8.5,500000,crime\n'
        'tt9300002,Candidate One,2021,Movie,"Crime,Thriller",Director X,7.8,80000,crime detective\n'
        'tt9300003,Candidate Two,2022,Movie,"Crime,Thriller",Director X,7.7,70000,crime mystery\n'
        'tt9300004,Candidate Three,2023,Movie,"Crime,Thriller",Director X,7.6,60000,crime investigation\n',
        encoding='utf-8'
    )
    import_catalog_csv(db, cat)
    primary, backups = RecommendationEngine(db).decision_pick(date(2026,9,14))
    assert primary is not None
    assert primary.movie.imdb_id != 'tt9300001'
    assert len(backups) == 2
    assert all(x.movie.imdb_id != 'tt9300001' for x in backups)
    assert 1.0 <= primary.score.predicted_rating <= 10.0
    assert 0.0 <= primary.score.confidence <= 1.0


def test_short_mode_filters_long_titles(tmp_path):
    db = dbtmp(tmp_path)
    p = tmp_path / 'ratings.csv'
    write_ratings(p, [rated('tt9400001', 9, 'Loved', 'History', 'Director X')])
    import_imdb_csv(db, p); build_profile(db)
    cat = tmp_path / 'catalog.csv'
    cat.write_text(
        'imdb_id,title,year,title_type,runtime,genres,directors,imdb_rating,num_votes,overview\n'
        'tt9400002,Short Choice,2022,Movie,95,History,Director X,7.2,10000,history\n'
        'tt9400003,Long Choice,2022,Movie,180,History,Director X,8.8,300000,history\n',
        encoding='utf-8'
    )
    import_catalog_csv(db, cat)
    recs = RecommendationEngine(db).recommend(date(2026,9,14), 3, mode='short', candidate_limit=1000)
    assert recs
    assert all((r.movie.runtime_min or 0) <= 120 for r in recs)
