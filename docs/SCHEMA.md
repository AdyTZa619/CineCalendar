# Schema SQLite

Versiunea curentă a schemei: **5**. Migrațiile canonice sunt definite în `cinecalendar/db.py`; `schema.sql` este fotografia lizibilă a aceleiași structuri.

## movies
Identitatea și metadatele filmului: IMDb ID, identity key, titluri normalizate, an, tip, runtime, genuri, regizori, țări, overview, keywords, semantic vector, IMDb rating/voturi, poster, TMDb ID și sursă.

## ratings
Un singur rating 1–10 per `movie_id` (`UNIQUE`). Modificarea unui rating actualizează aceeași intrare.

## feedback
Feedback explicit separat de rezultatul de playback: want-to-watch, seen, not-interested, more/less-like-this etc.

## watchlist
Titlurile păstrate pentru vizionare.

## recommendation_history
Expuneri și evenimente de interacțiune. Din v5, `exposure_history_id` leagă un eveniment ulterior (`stremio_opened`, `playback_confirmed`, `watched`, `skip_today` etc.) de recomandarea exactă care l-a produs.

Un eveniment nu trebuie să suprascrie alt eveniment mai puternic. De exemplu, feedbackul `seen` nu înlocuiește un rând `watched`.

## recommendation_runs
O rulare a motorului: dată/context, număr de candidați, număr de rezultate și versiunea reală a motorului folosit.

## recommendation_trust_audit
Snapshot pentru fiecare recomandare vizibilă V16: history/run ID, rang, versiune motor, `trusted/backfill/red_flag/bypassed`, trust score, gate score, semnale de susținere, red flag, gap, ALS și rating public bayesian.

## user_profile
Profil derivat JSON. Poate fi reconstruit din ratinguri și feedback și este recalculat după importul unui backup.

## import_files
Metadate/hash pentru deduplicarea importurilor locale. Nu reprezintă date personale esențiale pentru backupul de profil.

## metadata_cache
Cache provider/key cu payload și expirare.

## settings
Setări JSON key/value. Secretele și stările strict tranzitorii sunt excluse din backupul de profil.

## calendar_events
Persistență pentru evenimente calendaristice custom/extinse; calendarul de bază rămâne determinist.

## schema_migrations
Lista versiunilor de schemă aplicate.
