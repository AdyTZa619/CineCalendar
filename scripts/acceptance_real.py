from __future__ import annotations
import json, os, sys, tempfile
from datetime import date
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from cinecalendar.calendar_engine import CalendarEngine, orthodox_easter, seasonal_turning_dates
from cinecalendar.db import Database
from cinecalendar.imdb_import import import_imdb_csv
from cinecalendar.profile import build_profile, top_profile_features
from cinecalendar.recommendation import RecommendationEngine
from cinecalendar.util import sha256_file


def run(csv_path: Path, out_path: Path | None=None) -> dict:
    with tempfile.TemporaryDirectory(prefix='cinecalendar-acceptance-') as td:
        db=Database(Path(td)/'acceptance.db')
        res=import_imdb_csv(db,csv_path)
        profile=build_profile(db)
        with db.connect() as con:
            ratings=con.execute('SELECT COUNT(*) FROM ratings').fetchone()[0]
            movies=con.execute('SELECT COUNT(*) FROM movies').fetchone()[0]
            imdb_dupes=con.execute("SELECT COUNT(*) FROM (SELECT imdb_id,COUNT(*) n FROM movies WHERE imdb_id IS NOT NULL GROUP BY imdb_id HAVING n>1)").fetchone()[0]
            rating_dupes=con.execute("SELECT COUNT(*) FROM (SELECT movie_id,COUNT(*) n FROM ratings GROUP BY movie_id HAVING n>1)").fetchone()[0]
            unseen=con.execute("SELECT COUNT(*) FROM movies m WHERE NOT EXISTS(SELECT 1 FROM ratings r WHERE r.movie_id=m.id)").fetchone()[0]
        ce=CalendarEngine(); today=date(2026,9,13)
        groups=ce.month_groups(today)
        recs=RecommendationEngine(db,ce).recommend(today,3,record=False)
        report={
            'run_date':'2026-09-13',
            'input':{'name':csv_path.name,'sha256':sha256_file(csv_path),'rows':res.total_rows},
            'database':{'ratings':ratings,'movies':movies,'unseen_candidates':unseen,'duplicate_imdb_ids':imdb_dupes,'duplicate_rating_movie_ids':rating_dupes,'schema_version':4},
            'calendar':{
                'orthodox_easter_2026':orthodox_easter(2026).isoformat(),
                'ascension_2026':(orthodox_easter(2026).fromordinal(orthodox_easter(2026).toordinal()+39)).isoformat(),
                'pentecost_2026':(orthodox_easter(2026).fromordinal(orthodox_easter(2026).toordinal()+49)).isoformat(),
                'autumn_equinox_2026':seasonal_turning_dates(2026)['autumn_equinox'].isoformat(),
                'month_groups':[{'start':a.isoformat(),'end':b.isoformat(),'label':label} for a,b,label in groups],
            },
            'profile':{
                'rated_count':profile.get('rated_count'),
                'mean_user_minus_imdb':profile.get('mean_user_minus_imdb'),
                'top_genres':[(n,s.get('mean_rating'),s.get('count'),s.get('preference')) for n,s in top_profile_features(profile,'genre:',True,10)],
                'top_directors':[(n,s.get('mean_rating'),s.get('count'),s.get('preference')) for n,s in top_profile_features(profile,'director:',True,10)],
            },
            'recommendations_today':[
                {'title':r.movie.title,'year':r.movie.year,'score':round(r.score.final,4),'calendar_kind':r.score.calendar_kind,'calendar_reason':r.score.calendar_reason}
                for r in recs
            ],
            'recommendation_acceptance_status':'passed' if len(recs)==3 else 'blocked_no_unseen_catalog',
            'note':'ratings.csv contains evaluated titles, not an unseen candidate catalog. The application deliberately does not fabricate candidates.' if len(recs)<3 else '',
        }
    if out_path:
        out_path.parent.mkdir(parents=True,exist_ok=True)
        out_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

if __name__=='__main__':
    if len(sys.argv)<2:
        raise SystemExit('Usage: python scripts/acceptance_real.py <ratings.csv> [report.json]')
    report=run(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv)>2 else None)
    print(json.dumps(report,ensure_ascii=False,indent=2))
