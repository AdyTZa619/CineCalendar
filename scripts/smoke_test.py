from __future__ import annotations
import os, tempfile, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cinecalendar.service import CineCalendarService
from cinecalendar.util import AppPaths
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.profile import build_profile

def main():
    root=Path(tempfile.mkdtemp(prefix='cinecalendar-smoke-'))
    paths=AppPaths(root,root/'data',root/'logs',root/'cache',root/'backups')
    for p in (paths.data,paths.logs,paths.cache,paths.backups):p.mkdir(parents=True,exist_ok=True)
    a=CineCalendarService(paths);add_manual_rating(a.db,'Smoke Test',2026,8,'tt9999998',['Drama']);build_profile(a.db)
    # Restart service against the same portable folder.
    b=CineCalendarService(paths)
    with b.db.connect() as c:
        assert c.execute('select count(*) from ratings').fetchone()[0]==1
        assert c.execute('select count(*) from schema_migrations').fetchone()[0]>=3
    print('SMOKE_OK',root)
if __name__=='__main__':main()
