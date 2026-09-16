# Schema SQLite

Versiunea curentă a schemei: **5**. Migrațiile canonice sunt definite în `cinecalendar/db.py`; `schema.sql` rămâne fotografia lizibilă a structurii.

## movies
Identitatea și metadatele filmului: IMDb ID, identity key, titluri normalizate, an, tip, runtime, genuri, regizori, țări, overview, keywords, semantic vector, IMDb rating/voturi, poster, TMDb ID și sursă.

## ratings
Un singur rating 1–10 per `movie_id` (`UNIQUE`).

## feedback
Feedback explicit separat de recommendation history. Din 3.3, salvarea feedbackului nu mai mută sau rescrie o expunere de recomandare.

## watchlist
Titlurile păstrate pentru vizionare.

## recommendation_history
Are două roluri structurale distincte:

- **expunere-rădăcină**: `exposure_history_id IS NULL`, cardul concret afișat utilizatorului;
- **eveniment copil**: `exposure_history_id` indică expunerea-rădăcină care a produs acțiunea.

În 3.3 expunerile noi sunt imuabile. `chosen`, `skip_today`, `trailer_opened`, `stremio_opened`, `playback_confirmed` și `watched` se adaugă ca evenimente separate; nu se schimbă `recommended_at`, scorul sau acțiunea expunerii originale.

Datele istorice 3.2 în care `chosen/skip` au fost scrise direct pe root rămân acceptate de audit pentru compatibilitate.

## recommendation_runs
O rulare a motorului: context, număr de candidați/rezultate și versiunea motorului.

## recommendation_trust_audit
Snapshot pentru fiecare expunere vizibilă V16: history/run ID, rang, versiune motor, `trusted/backfill/red_flag/bypassed`, trust score, gate score, supports, red flag, gap, ALS și rating public bayesian.

## user_profile
Profil derivat JSON, rebuildabil din ratinguri și feedback.

## import_files
Metadate/hash pentru deduplicarea importurilor locale.

## metadata_cache
Cache provider/key cu payload și expirare.

## settings
Setări JSON. Secretele și stările strict tranzitorii sunt excluse din backupul de profil.

## calendar_events
Persistență pentru evenimente custom/extinse.

## schema_migrations
Lista versiunilor aplicate. Din 3.3, pașii v2-v5 sunt reconciliați idempotent și tranzacțional; înaintea unei schimbări necesare se creează `cinecalendar.pre_migration.bak`, iar integritatea este verificată cu `foreign_key_check` și `quick_check`.
