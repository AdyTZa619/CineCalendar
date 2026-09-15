from pathlib import Path
from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.profile import build_profile
from cinecalendar.backup import export_profile,import_profile

def test_backup_roundtrip(tmp_path):
    a=Database(tmp_path/'a.db');add_manual_rating(a,'Test',2020,8,'tt9999999',['Drama']);build_profile(a)
    z=export_profile(a,tmp_path/'p.zip');b=Database(tmp_path/'b.db');r=import_profile(b,z)
    with b.connect() as c: assert c.execute('select count(*) from ratings').fetchone()[0]==1


def test_backup_excludes_tmdb_token(tmp_path):
    import json, zipfile
    a=Database(tmp_path/'a.db');a.set_setting('tmdb_token','secret-token');a.set_setting('theme','dark')
    z=export_profile(a,tmp_path/'p.zip')
    with zipfile.ZipFile(z,'r') as fh: payload=json.loads(fh.read('profile.json').decode('utf-8'))
    settings={x['key'] for x in payload['tables']['settings']}
    assert 'tmdb_token' not in settings and 'theme' in settings
