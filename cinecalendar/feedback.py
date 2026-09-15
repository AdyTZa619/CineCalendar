from __future__ import annotations
from .db import Database
from .profile import build_profile
from .util import utcnow_iso

FEEDBACK_WEIGHTS={
    "want_to_watch": .04,
    "not_interested": -.10,
    "never_similar": -.20,
    "more_like_this": .10,
    "less_like_this": -.10,
}

def apply_feedback(db:Database,movie_id:int,kind:str):
    now=utcnow_iso()
    if kind=="seen":
        # Seen without an explicit 1-10 rating suppresses future recommendation but does not invent a rating.
        with db.tx() as con:
            con.execute("INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,0,?)",(movie_id,kind,now))
            con.execute("UPDATE recommendation_history SET action='seen' WHERE movie_id=? AND id=(SELECT id FROM recommendation_history WHERE movie_id=? ORDER BY id DESC LIMIT 1)",(movie_id,movie_id))
        return build_profile(db)
    if kind not in FEEDBACK_WEIGHTS: raise ValueError("Tip feedback necunoscut")
    w=FEEDBACK_WEIGHTS[kind]
    with db.tx() as con:
        con.execute("INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",(movie_id,kind,w,now))
        if kind=="want_to_watch":
            con.execute("""INSERT INTO watchlist(movie_id,status,added_at,updated_at) VALUES(?,'want_to_watch',?,?)
                         ON CONFLICT(movie_id) DO UPDATE SET status='want_to_watch',updated_at=excluded.updated_at""",(movie_id,now,now))
        con.execute("UPDATE recommendation_history SET action=? WHERE movie_id=? AND id=(SELECT id FROM recommendation_history WHERE movie_id=? ORDER BY id DESC LIMIT 1)",(kind,movie_id,movie_id))
    return build_profile(db)
