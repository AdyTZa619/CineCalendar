from __future__ import annotations
import csv, os, tempfile
from datetime import date
from pathlib import Path
import pytest
from cinecalendar.db import Database
from cinecalendar.imdb_import import import_imdb_csv, add_manual_rating, validate_imdb_csv
from cinecalendar.profile import build_profile
from cinecalendar.calendar_engine import orthodox_easter, CalendarEngine, seasonal_turning_dates
from cinecalendar.catalog import import_catalog_csv
from cinecalendar.recommendation import RecommendationEngine, romance_policy
from cinecalendar.feedback import apply_feedback
from cinecalendar.models import Movie

HEADERS=['Const','Your Rating','Date Rated','Title','Original Title','URL','Title Type','IMDb Rating','Runtime (mins)','Year','Genres','Num Votes','Release Date','Directors']

def dbtmp(tmp_path): return Database(tmp_path/'test.db')

def write_ratings(path, rows, headers=HEADERS):
    with open(path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=headers);w.writeheader();
        for r in rows:w.writerow(r)

def row(i='tt0000001',rating='8',title='Film',year='2000',genres='Drama',directors='Regizor'):
    return {'Const':i,'Your Rating':rating,'Date Rated':'2026-09-01','Title':title,'Original Title':title,'URL':'','Title Type':'Movie','IMDb Rating':'7.0','Runtime (mins)':'100','Year':year,'Genres':genres,'Num Votes':'10000','Release Date':'2000-01-01','Directors':directors}

def test_import_imdb_and_duplicate_hash(tmp_path):
    db=dbtmp(tmp_path); p=tmp_path/'ratings.csv';write_ratings(p,[row(),row('tt0000002','5','Alt film','2001','Comedy')])
    r=import_imdb_csv(db,p);assert r.total_rows==2;assert len(r.new_ratings)==2
    r2=import_imdb_csv(db,p);assert r2.skipped_same_file
    with db.connect() as c:assert c.execute('select count(*) from ratings').fetchone()[0]==2

def test_column_order_and_diacritics(tmp_path):
    db=dbtmp(tmp_path); p=tmp_path/'r.csv'; headers=list(reversed(HEADERS)); rr=row(title='Pădurea spânzuraților',directors='Liviu Ciulei')
    write_ratings(p,[rr],headers);import_imdb_csv(db,p)
    with db.connect() as c:
        x=c.execute('select title from movies').fetchone()[0];assert x=='Pădurea spânzuraților'

def test_invalid_csv(tmp_path):
    p=tmp_path/'bad.csv';p.write_text('Title,Year\nX,2000\n',encoding='utf-8')
    with pytest.raises(ValueError):validate_imdb_csv(p)

def test_manual_reconciles_with_imdb(tmp_path):
    db=dbtmp(tmp_path);mid=add_manual_rating(db,'Același film',1999,7,None,['Drama'])
    p=tmp_path/'r.csv';write_ratings(p,[row('tt1234567','9','Același film','1999','Drama')]);r=import_imdb_csv(db,p)
    assert r.merged_manual==1
    with db.connect() as c:
        assert c.execute('select count(*) from movies').fetchone()[0]==1
        x=c.execute('select m.imdb_id,r.rating from movies m join ratings r on r.movie_id=m.id').fetchone();assert x[0]=='tt1234567' and x[1]==9


def test_manual_reconciles_when_imdb_original_title_differs(tmp_path):
    db=dbtmp(tmp_path);add_manual_rating(db,'Titlu românesc',1999,7,None,['Drama'])
    rr=row('tt7654321','9','Titlu românesc','1999','Drama')
    rr['Original Title']='Different Original Title'
    p=tmp_path/'r_original.csv';write_ratings(p,[rr]);res=import_imdb_csv(db,p)
    assert res.merged_manual==1
    with db.connect() as c:
        assert c.execute('select count(*) from movies').fetchone()[0]==1
        x=c.execute('select m.imdb_id,r.rating,m.original_title from movies m join ratings r on r.movie_id=m.id').fetchone()
        assert x[0]=='tt7654321' and x[1]==9 and x[2]=='Different Original Title'

def test_rating_modification_updates_not_duplicates(tmp_path):
    db=dbtmp(tmp_path);p1=tmp_path/'a.csv';p2=tmp_path/'b.csv';write_ratings(p1,[row(rating='7')]);write_ratings(p2,[row(rating='9')])
    import_imdb_csv(db,p1);r=import_imdb_csv(db,p2);assert r.changed_ratings==[('Film',7,9)]
    with db.connect() as c:assert c.execute('select count(*) from ratings').fetchone()[0]==1

def test_orthodox_easter_known_dates():
    assert orthodox_easter(2024)==date(2024,5,5)
    assert orthodox_easter(2025)==date(2025,4,20)
    assert orthodox_easter(2026)==date(2026,4,12)
    assert orthodox_easter(2027)==date(2027,5,2)

def test_mobile_feasts_2026():
    e=CalendarEngine().events_for_year(2026);d={x.key:x.start for x in e}
    assert d['palm_sunday']==date(2026,4,5)
    assert d['ascension']==date(2026,5,21)
    assert d['pentecost']==date(2026,5,31)

def test_exaltation_is_active_sep13_and_not_passion_direct():
    ce=CalendarEngine();ev=[e for e,_ in ce.relevant_events(date(2026,9,13))]
    assert any(x.key=='exaltation_cross' for x in ev)
    m=Movie(title='Patimile',genres=['Drama'],overview='The crucifixion and Passion of Christ',semantic={'passion_of_christ':1,'christianity':.8,'faith':.7})
    score,kind,_=ce.calendar_relevance(m,date(2026,9,14));assert kind!='directă';assert score<.6

def test_2026_autumn_equinox_calendar_day():
    assert seasonal_turning_dates(2026)['autumn_equinox']==date(2026,9,23)

def test_romance_filter():
    hard=Movie(title='Love',genres=['Drama','Romance'],semantic={'romance':1})
    assert romance_policy(hard,True)[0] is False
    secondary=Movie(title='War and Love',genres=['Drama','Romance','War'],semantic={'romance':.5,'war':1,'history':.8})
    ok,pen,_=romance_policy(secondary,True);assert ok and pen>0

def test_rated_titles_never_candidates(tmp_path):
    db=dbtmp(tmp_path);p=tmp_path/'r.csv';write_ratings(p,[row('tt0000001','9','Seen Film','2000','History, War')]);import_imdb_csv(db,p);build_profile(db)
    cat=tmp_path/'cat.csv';cat.write_text('imdb_id,title,year,title_type,genres,imdb_rating,num_votes,overview\ntt0000001,Seen Film,2000,Movie,"History,War",8.0,100000,war history\ntt0000002,New Film,2001,Movie,"History,War",7.8,90000,war history\n',encoding='utf-8')
    import_catalog_csv(db,cat);recs=RecommendationEngine(db).recommend(date(2026,9,21),3)
    assert all(x.movie.imdb_id!='tt0000001' for x in recs)

def test_feedback_not_interested_suppresses_movie(tmp_path):
    db=dbtmp(tmp_path);p=tmp_path/'r.csv';write_ratings(p,[row('tt0000100','9','Rated','2000','History, War')]);import_imdb_csv(db,p);build_profile(db)
    cat=tmp_path/'cat.csv';cat.write_text('imdb_id,title,year,title_type,genres,imdb_rating,num_votes,overview\ntt0000200,Candidate A,2001,Movie,"History,War",8.0,100000,war\ntt0000201,Candidate B,2002,Movie,"History,War",7.9,90000,war\n',encoding='utf-8');import_catalog_csv(db,cat)
    r=RecommendationEngine(db).recommend(date(2026,9,21),1)[0];apply_feedback(db,r.movie.id,'not_interested')
    r2=RecommendationEngine(db).recommend(date(2026,9,21),2);assert all(x.movie.id!=r.movie.id for x in r2)

def test_repeat_history_penalty(tmp_path):
    db=dbtmp(tmp_path);p=tmp_path/'r.csv';write_ratings(p,[row('tt0000100','9','Rated','2000','History, War')]);import_imdb_csv(db,p);build_profile(db)
    cat=tmp_path/'cat.csv';cat.write_text('imdb_id,title,year,title_type,genres,imdb_rating,num_votes,overview\ntt0000200,Candidate A,2001,Movie,"History,War",8.0,100000,war\n',encoding='utf-8');import_catalog_csv(db,cat)
    eng=RecommendationEngine(db);first=eng.recommend(date(2026,9,21),1)[0]
    with db.tx() as c:c.execute("insert into recommendation_history(movie_id,recommended_at,context_date,slot,final_score) values(?,?,?,?,?)",(first.movie.id,'2026-09-20T10:00:00+00:00','2026-09-20','today',first.score.final))
    second=eng.recommend(date(2026,9,21),1)[0];assert second.score.repeat_penalty>=.2 and second.score.final<first.score.final

def test_real_imdb_acceptance_if_available(tmp_path):
    real=os.environ.get('CINECALENDAR_REAL_RATINGS')
    if not real or not Path(real).exists():pytest.skip('real user export not provided')
    db=dbtmp(tmp_path);res=import_imdb_csv(db,real);build_profile(db)
    with db.connect() as c:
        count=c.execute('select count(*) from ratings').fetchone()[0]
        duplicates=c.execute('select count(*) from (select movie_id,count(*) n from ratings group by movie_id having n>1)').fetchone()[0]
    assert count==res.total_rows;assert duplicates==0

def test_september_2026_month_groups_are_event_driven():
    groups=CalendarEngine().month_groups(date(2026,9,13))
    assert [(a,b) for a,b,_ in groups]==[
        (date(2026,9,13),date(2026,9,15)),
        (date(2026,9,16),date(2026,9,22)),
        (date(2026,9,23),date(2026,9,26)),
        (date(2026,9,27),date(2026,9,30)),
    ]

def test_official_catalog_auto_download_and_validate(tmp_path, monkeypatch):
    import io, gzip
    from cinecalendar import catalog as cat
    basics_text=(
        'tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres\n'
        'tt9000001\tmovie\tAuto Film\tAuto Film\t0\t2020\t\\N\t100\tDrama,History\n'
    ).encode('utf-8')
    ratings_text=(
        'tconst\taverageRating\tnumVotes\n'
        'tt9000001\t8.1\t1000\n'
    ).encode('utf-8')
    def gz(data):
        b=io.BytesIO()
        with gzip.GzipFile(fileobj=b,mode='wb') as f:f.write(data)
        return b.getvalue()
    payloads={cat.IMDB_DATASET_URLS['basics']:gz(basics_text),cat.IMDB_DATASET_URLS['ratings']:gz(ratings_text)}
    class Resp:
        def __init__(self,data):self.data=data;self.status_code=200;self.headers={'Content-Length':str(len(data))}
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def raise_for_status(self):pass
        def iter_content(self,chunk_size=1024):
            for i in range(0,len(self.data),chunk_size):yield self.data[i:i+chunk_size]
    monkeypatch.setattr(cat.requests,'get',lambda url,**kwargs:Resp(payloads[url]))
    basics,ratings=cat.download_official_imdb_datasets(tmp_path/'cache')
    assert basics.exists() and ratings.exists()
    db=dbtmp(tmp_path)
    result=cat.import_imdb_datasets(db,basics,ratings,min_votes=50)
    assert result['movies']==1
    with db.connect() as con:
        r=con.execute("select imdb_id,title,imdb_rating from movies").fetchone()
        assert tuple(r)==('tt9000001','Auto Film',8.1)
