from __future__ import annotations

from typing import Iterable

from .util import utcnow_iso


def record_metadata_source(con, movie_id: int, field: str, provider: str, *, updated_at: str | None = None) -> None:
    """Record the latest provider that supplied one metadata field."""
    field = str(field or "").strip()
    provider = str(provider or "").strip()
    if not field or not provider:
        return
    con.execute(
        """INSERT INTO metadata_provenance(movie_id,field,provider,updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(movie_id,field) DO UPDATE SET
             provider=excluded.provider,
             updated_at=excluded.updated_at""",
        (int(movie_id), field, provider, updated_at or utcnow_iso()),
    )


def record_metadata_sources(
    con,
    movie_id: int,
    fields: Iterable[str],
    provider: str,
    *,
    updated_at: str | None = None,
) -> None:
    stamp = updated_at or utcnow_iso()
    for field in fields:
        record_metadata_source(con, movie_id, field, provider, updated_at=stamp)


def metadata_sources_for_movie(db, movie_id: int) -> dict[str, str]:
    with db.connect() as con:
        rows = con.execute(
            """SELECT field,provider
               FROM metadata_provenance
               WHERE movie_id=?
               ORDER BY field""",
            (int(movie_id),),
        ).fetchall()
    return {str(row["field"]): str(row["provider"]) for row in rows}
