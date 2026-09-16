# CineCalendar

CineCalendar este o aplicație Windows portabilă pentru recomandări personale de filme. Istoricul real de ratinguri rămâne sursa principală de adevăr pentru gust; calendarul ortodox/secular/sezonier, ALS, modelul adaptiv și semnalele de vizionare rafinează selecția fără să înlocuiască profilul personal.

## Flux normal

1. Pornești `CineCalendar.exe` din folderul portabil.
2. Importi exportul IMDb `ratings.csv`.
3. Dacă baza locală nu are încă suficiente filme nevăzute, aplicația construiește catalogul din dataseturile oficiale IMDb.
4. „Ce văd acum?” produce o alegere principală și alternative scurte, iar „Recomandări” oferă o listă mai largă.

Datele sunt păstrate în `CineCalendarData` lângă bundle. Update-ul nu șterge acest director.

## Motorul curent

Serviciul de producție folosește `FastRecommendationEngineV16`, compus cu:

- retrieval personal ALS + vecini ai favoritelor + discovery;
- profil personal pe termen lung din ratingurile 1–10;
- Adaptive Personal v2 cu validare temporală și influență plafonată;
- calendar ortodox/secular/sezonier ca semnal contextual;
- Watch Success v3.3 pe funnel-uri de **expunere**, nu pe `film + zi`;
- Startability ca departajare limitată între candidați deja competitivi;
- Top-3 trust gate V16 pentru setul mic de decizie.

Nota „pentru tine”, încrederea, Startability și trust gate sunt semnale distincte.

## Fundația de integritate 3.3

Versiunea 3.3 face identitatea recomandării explicită cap-coadă:

- fiecare card vizibil primește un `exposure_history_id` concret;
- alegerea, skip-ul, trailerul, Stremio, confirmarea de playback și `watched` păstrează acel ID;
- dacă același film apare din nou în aceeași zi, aplicația nu mai alege automat „cea mai recentă” expunere;
- expunerea originală este **imutabilă**: acțiunile sunt rânduri separate legate de ea;
- feedbackul din tabela `feedback` nu mai modifică rândul original al recomandării;
- auditul Watch Success lucrează per expunere și raportează separat legăturile legacy ambigue.

Datele 3.2 rămân compatibile. Cazurile istorice fără ID explicit sunt recuperate numai când legătura este neambiguă.

## SQLite și migrare

Schema curentă rămâne **v5**. În 3.3 s-a schimbat mecanismul de aplicare a migrărilor:

- pașii cu `ALTER TABLE` sunt verificați înainte de aplicare;
- migrarea poate relua în siguranță o schemă parțial modificată;
- versiunea migrării este înscrisă în aceeași tranzacție cu modificarea;
- înaintea unei migrări necesare se face snapshot SQLite consistent;
- după migrare se rulează `foreign_key_check` și `quick_check`;
- dacă migrarea eșuează, snapshotul local este restaurat înainte ca eroarea să fie propagată.

## Backup profil

Formatul de profil este v2 și nu copiază întregul catalog IMDb rebuildabil. Include filmele referite de starea utilizatorului, ratingurile, feedbackul, watchlist-ul, recommendation history/runs și trust telemetry.

`import_profile(..., mode="merge")` este modul implicit și sigur: un backup mai vechi nu suprascrie ratinguri, setări sau watchlist mai noi. `mode="restore"` este explicit și face backupul autoritar pentru starea utilizatorului. Tokenul TMDb și stările tranzitorii nu sunt exportate.

## Updater Windows

Updaterul `onedir`:

1. descarcă ZIP-ul stable;
2. verifică SHA-256;
3. extrage într-un staging sigur;
4. face backup la bundle-ul curent;
5. creează și un snapshot SQLite consistent înainte de handoff;
6. pornește versiunea nouă și așteaptă health-check;
7. la succes șterge backupurile temporare;
8. la eșec restaurează **atât bundle-ul, cât și baza SQLite**, apoi repornește versiunea anterioară.

## Build și validare

```bat
scripts\build_windows.bat
```

Build-ul oficial și cel local folosesc PyInstaller `--onedir`. CI rulează testele, benchmark-ul pe catalog mare, build-ul Windows și smoke launch înainte de publicarea Stable.

Pentru audit local:

```bash
python scripts/audit_watch_success.py "C:\cale\CineCalendarData\data\cinecalendar.db"
```

Pragurile V16 nu sunt auto-reglate din câteva clickuri. Recalibrarea trebuie făcută numai după suficiente rezultate reale și legături exacte.

## Limitări

Vezi `docs/LIMITATIONS.md`.


### Fundație 3.3
CineCalendar 3.3 adaugă identitate exactă a expunerilor, evenimente append-only, audit pe expunere, migrații idempotente, quick-check/recovery SQLite, backup WAL consistent, rollback comun bundle+DB, single-instance Windows și compoziție UI canonică verificată.
