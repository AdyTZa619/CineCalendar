# Schema SQLite

Versiunea curentă a schemei: 3. Migrațiile sunt definite în `cinecalendar/db.py`.

## movies
Identitatea și metadatele filmului: IMDb ID, identity key robust, titluri, an, tip, runtime, genuri, regizori, țări, overview, keywords, semantic vector, IMDb rating, număr voturi, release date, poster, sursă, TMDb ID.

## ratings
Un singur rating 1–10 per `movie_id`. `movie_id` este `UNIQUE`, deci modificarea unui rating actualizează aceeași intrare.

## import_files
Hash SHA-256, dimensiune, mtime, număr de rânduri și timestamp de import pentru deduplicarea exporturilor.

## user_profile
Profil serializat JSON, recalculat după import/rating/feedback.

## recommendation_history / recommendation_runs
Persistă expunerea, contextul, scorul și acțiunea utilizatorului.

## feedback
Evenimente moderate: want_to_watch, not_interested, never_similar, more_like_this, less_like_this, seen.

## watchlist
Titluri marcate pentru vizionare.

## metadata_cache
Cache provider/key cu payload și expirare.

## settings
Setări JSON key/value.

## calendar_events
Rezervat pentru custom events/persistență extinsă; calendarul de bază este calculat determinist.

## schema_migrations
Versiuni aplicate.
