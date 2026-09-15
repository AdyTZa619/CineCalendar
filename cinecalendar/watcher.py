from __future__ import annotations
from pathlib import Path
from .db import Database
from .imdb_import import validate_imdb_csv, import_imdb_csv, ImportResult
from .profile import build_profile
from .util import sha256_file

class RatingsFolderWatcher:
    def __init__(self,db:Database,folder:str|Path):
        self.db=db; self.folder=Path(folder).expanduser()

    def scan(self)->list[ImportResult]:
        if not self.folder.exists(): return []
        results=[]
        files=sorted(self.folder.glob("*.csv"),key=lambda p:p.stat().st_mtime,reverse=True)
        # Usually only the newest export matters, but validate up to 10 recent CSVs to avoid assuming a filename.
        for p in files[:10]:
            st=p.stat()
            with self.db.connect() as con:
                cheap=con.execute("SELECT sha256 FROM import_files WHERE path_name=? AND size_bytes=? AND mtime_ns=?",(p.name,st.st_size,st.st_mtime_ns)).fetchone()
            if cheap: continue
            try:
                validate_imdb_csv(p)
            except Exception:
                continue
            digest=sha256_file(p)
            with self.db.connect() as con:
                if con.execute("SELECT 1 FROM import_files WHERE sha256=?",(digest,)).fetchone(): continue
            res=import_imdb_csv(self.db,p); results.append(res)
            if res.changed: build_profile(self.db)
            break
        return results
