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
    # Contextual feedback: stored for short-horizon learning, but deliberately does not
    # alter the long-term taste profile.
    "not_now": 0.0,
    "too_long": 0.0,
    "mood_mismatch": 0.0,
    "too_similar": 0.0,
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
            # A watched title no longer belongs in an explicit "want to watch" queue.
            con.execute("DELETE FROM watchlist WHERE movie_id=?", (movie_id,))
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
        elif kind in {"not_interested", "never_similar"}:
            # Explicit negative intent and "want to watch" cannot both be active.
            con.execute("DELETE FROM watchlist WHERE movie_id=?", (movie_id,))
    return build_profile(db)
