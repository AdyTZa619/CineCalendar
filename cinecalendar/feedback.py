from __future__ import annotations

from .db import Database
from .profile import build_profile
from .util import utcnow_iso


FEEDBACK_WEIGHTS = {
    "want_to_watch": .04,
    "not_interested": -.10,
    "never_similar": -.20,
    "more_like_this": .10,
    "less_like_this": -.10,
}


def apply_feedback(db: Database, movie_id: int, kind: str):
    """Persist explicit feedback without mutating recommendation exposure rows.

    Since v3.3 recommendation_history root rows are immutable evidence of what was shown. Feedback
    belongs in the feedback/watchlist tables; watch/decision events are appended separately.
    """
    now = utcnow_iso()
    movie_id = int(movie_id)
    if kind == "seen":
        with db.tx() as con:
            con.execute(
                "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,0,?)",
                (movie_id, kind, now),
            )
        return build_profile(db)

    if kind not in FEEDBACK_WEIGHTS:
        raise ValueError("Tip feedback necunoscut")
    weight = FEEDBACK_WEIGHTS[kind]
    with db.tx() as con:
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (movie_id, kind, weight, now),
        )
        if kind == "want_to_watch":
            con.execute(
                """INSERT INTO watchlist(movie_id,status,added_at,updated_at)
                   VALUES(?,'want_to_watch',?,?)
                   ON CONFLICT(movie_id) DO UPDATE SET
                     status='want_to_watch',updated_at=excluded.updated_at""",
                (movie_id, now, now),
            )
    return build_profile(db)
