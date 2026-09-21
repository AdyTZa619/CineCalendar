from __future__ import annotations

from dataclasses import dataclass

from .db import Database
from .profile import build_profile
from .util import utcnow_iso


FEEDBACK_WEIGHTS = {
    "want_to_watch": .04,
    # Title-only dismissal.  Similar-title learning is deliberately reserved for the explicit
    # less_like_this / never_similar actions; otherwise hiding one film poisons every feature it
    # happens to share with future candidates.
    "not_interested": 0.0,
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


FEEDBACK_LABELS = {
    "seen": "Am văzut",
    "want_to_watch": "Vreau să văd",
    "not_interested": "Ascunde doar filmul",
    "never_similar": "Nu-mi recomanda similare",
    "more_like_this": "Mai multe ca acesta",
    "less_like_this": "Mai puține ca acesta",
    "not_now": "Nu acum",
    "too_long": "Prea lung pentru moment",
    "mood_mismatch": "Nu am chef de genul acesta acum",
    "too_similar": "Prea similar cu ce am văzut recent",
}


@dataclass(frozen=True)
class FeedbackReceipt:
    feedback_id: int
    movie_id: int
    kind: str
    created_at: str

    @property
    def label(self) -> str:
        return FEEDBACK_LABELS.get(self.kind, self.kind)


_WATCHLIST_STATE_KINDS = {"want_to_watch", "not_interested", "never_similar", "seen"}


def _reconcile_watchlist(con, movie_id: int, now: str) -> None:
    """Rebuild the derived watchlist state from the remaining feedback event stream."""
    marks = ",".join("?" for _ in _WATCHLIST_STATE_KINDS)
    latest = con.execute(
        f"""SELECT kind,created_at FROM feedback
            WHERE movie_id=? AND kind IN ({marks})
            ORDER BY id DESC LIMIT 1""",
        (int(movie_id), *sorted(_WATCHLIST_STATE_KINDS)),
    ).fetchone()
    if latest is not None and str(latest["kind"]) == "want_to_watch":
        added_at = str(latest["created_at"] or now)
        con.execute(
            """INSERT INTO watchlist(movie_id,status,added_at,updated_at)
               VALUES(?,'want_to_watch',?,?)
               ON CONFLICT(movie_id) DO UPDATE SET
                 status='want_to_watch',updated_at=excluded.updated_at""",
            (int(movie_id), added_at, now),
        )
    else:
        con.execute("DELETE FROM watchlist WHERE movie_id=?", (int(movie_id),))


def _record_feedback(db: Database, movie_id: int, kind: str) -> FeedbackReceipt:
    """Persist explicit feedback without mutating recommendation exposure rows.

    Since v3.3 recommendation_history root rows are immutable evidence of what was shown. Feedback
    belongs in the feedback/watchlist tables; watch/decision events are appended separately.
    """
    now = utcnow_iso()
    movie_id = int(movie_id)
    if kind == "seen":
        with db.tx() as con:
            cur = con.execute(
                "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,0,?)",
                (movie_id, kind, now),
            )
            # A watched title no longer belongs in an explicit "want to watch" queue.
            con.execute("DELETE FROM watchlist WHERE movie_id=?", (movie_id,))
        return FeedbackReceipt(int(cur.lastrowid), movie_id, kind, now)

    if kind not in FEEDBACK_WEIGHTS:
        raise ValueError("Tip feedback necunoscut")
    weight = FEEDBACK_WEIGHTS[kind]
    with db.tx() as con:
        cur = con.execute(
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
    return FeedbackReceipt(int(cur.lastrowid), movie_id, kind, now)


def apply_feedback_with_receipt(db: Database, movie_id: int, kind: str) -> tuple[dict, FeedbackReceipt]:
    receipt = _record_feedback(db, movie_id, kind)
    return build_profile(db), receipt


def apply_feedback(db: Database, movie_id: int, kind: str):
    profile, _receipt = apply_feedback_with_receipt(db, movie_id, kind)
    return profile


def undo_feedback(db: Database, feedback_id: int) -> FeedbackReceipt:
    """Undo one exact feedback event and restore any derived Watchlist state.

    The operation is transactional and works after a page refresh because it targets the persisted
    feedback id rather than guessing from movie/title. Recommendation exposure history remains
    immutable; only the explicit feedback event is removed.
    """
    feedback_id = int(feedback_id)
    now = utcnow_iso()
    with db.tx() as con:
        row = con.execute(
            "SELECT id,movie_id,kind,created_at FROM feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Feedbackul nu mai există sau a fost deja anulat.")
        receipt = FeedbackReceipt(
            feedback_id=int(row["id"]),
            movie_id=int(row["movie_id"]),
            kind=str(row["kind"]),
            created_at=str(row["created_at"] or ""),
        )
        con.execute("DELETE FROM feedback WHERE id=?", (feedback_id,))
        _reconcile_watchlist(con, receipt.movie_id, now)
    build_profile(db)
    return receipt
