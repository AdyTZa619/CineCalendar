from __future__ import annotations
import json, zipfile
from pathlib import Path
from .db import Database
from .util import utcnow_iso

TABLES=["movies","ratings","feedback","settings","recommendation_history","watchlist","user_profile","import_files"]

def export_profile(db:Database,path:str|Path)->Path:
    path=Path(path)
    payload={"format":"CineCalendarProfile","version":1,"exported_at":utcnow_iso(),"tables":{}}
    with db.connect() as con:
        for table in TABLES:
            if table=="settings":
                rows=con.execute("SELECT * FROM settings WHERE key!='tmdb_token'").fetchall()
            else:
                rows=con.execute(f"SELECT * FROM {table}").fetchall()
            payload["tables"][table]=[dict(r) for r in rows]
    manifest={"format":payload["format"],"version":1,"exported_at":payload["exported_at"],"counts":{k:len(v) for k,v in payload["tables"].items()}}
    path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False,indent=2))
        z.writestr("profile.json",json.dumps(payload,ensure_ascii=False))
    return path


def import_profile(db:Database,path:str|Path)->dict:
    path=Path(path)
    with zipfile.ZipFile(path,"r") as z:
        payload=json.loads(z.read("profile.json").decode("utf-8"))
    if payload.get("format")!="CineCalendarProfile" or payload.get("version")!=1: raise ValueError("Backup incompatibil.")
    t=payload.get("tables",{})
    # Import is merge-safe for core user data. Movie IDs are preserved only when free; IMDb/identity matching is used otherwise.
    counts={}
    with db.tx() as con:
        idmap={}
        for m in t.get("movies",[]):
            existing=con.execute("SELECT id FROM movies WHERE imdb_id=?",(m.get("imdb_id"),)).fetchone() if m.get("imdb_id") else None
            if not existing: existing=con.execute("SELECT id FROM movies WHERE identity_key=? ORDER BY id LIMIT 1",(m.get("identity_key"),)).fetchone()
            if existing: new_id=existing["id"]
            else:
                cols=[k for k in m.keys() if k!="id"]; vals=[m[k] for k in cols]
                cur=con.execute(f"INSERT INTO movies({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",vals); new_id=cur.lastrowid
            idmap[m["id"]]=new_id
        for r in t.get("ratings",[]):
            mid=idmap.get(r["movie_id"]); 
            if not mid: continue
            con.execute("""INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)
                           ON CONFLICT(movie_id) DO UPDATE SET rating=excluded.rating,date_rated=excluded.date_rated,source=excluded.source,updated_at=excluded.updated_at""",
                        (mid,r["rating"],r.get("date_rated"),r.get("source","backup"),r.get("imported_at",utcnow_iso()),r.get("updated_at",utcnow_iso())))
        for f in t.get("feedback",[]):
            mid=idmap.get(f["movie_id"])
            if mid: con.execute("INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",(mid,f["kind"],f["weight"],f["created_at"]))
        for s in t.get("settings",[]):
            con.execute("INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",(s["key"],s["value_json"],s["updated_at"]))
        for w in t.get("watchlist",[]):
            mid=idmap.get(w["movie_id"])
            if mid: con.execute("INSERT OR REPLACE INTO watchlist(movie_id,status,added_at,updated_at) VALUES(?,?,?,?)",(mid,w["status"],w["added_at"],w["updated_at"]))
    from .profile import build_profile
    build_profile(db)
    return {"movies":len(idmap),"ratings":len(t.get("ratings",[])),"feedback":len(t.get("feedback",[]))}
