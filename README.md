# CineCalendar

CineCalendar este o aplicație Windows portabilă pentru recomandări personale de filme. Sursa principală de adevăr pentru gust este istoricul real de ratinguri al utilizatorului; calendarul ortodox/secular/sezonier, semnalele de intenție și calitatea publică sunt contexte suplimentare, nu înlocuitori pentru gust.

## Flux normal

1. Pornești `CineCalendar.exe` din folderul portabil.
2. Importi exportul IMDb `ratings.csv` din pagina „Ratinguri IMDb”.
3. Dacă baza locală nu are încă filme nevăzute, aplicația poate descărca automat dataseturile oficiale IMDb `title.basics.tsv.gz` și `title.ratings.tsv.gz`, le validează și construiește catalogul SQLite.
4. Pagina „Ce văd acum?” produce setul mic de recomandări pentru decizie; calendarul și programul lunar folosesc aceeași bază personală, cu context temporal suplimentar.

Datele aplicației sunt păstrate în `CineCalendarData` lângă bundle-ul portabil. Update-ul nu șterge acest director.

## Motorul de recomandare curent

Serviciul de producție folosește `FastRecommendationEngineV16`.

Pipeline-ul este intenționat stratificat:

- **retrieval personal**: când modelul colaborativ este disponibil, pool-ul combină candidați ALS, vecini ai favoritelor explicite și discovery generic;
- **profil personal pe termen lung**: ratingurile 1–10, genurile, regizorii, țările, perioadele, runtime-ul și semantica locală contribuie la potrivire;
- **Adaptive Personal v2**: model local cu validare temporală și influență plafonată; dacă validarea nu justifică modelul, motorul revine conservator la baza ALS + content;
- **calendar**: sărbători ortodoxe fixe/mobile, repere seculare/istorice și sezon, fără a domina gustul personal;
- **Watch Success / Startability**: semnale pe termen scurt pentru departajarea unor candidați deja buni; deschiderea Stremio nu este confundată cu pornirea filmului;
- **Top-3 trust gate (V16)**: înainte ca setul mic vizibil să fie afișat, sunt combinate mai multe semnale independente. Un candidat cu red flag nu este promovat peste unul sigur doar pentru un scor marginal mai bun.

Nota estimată „pentru tine” și semnalul „ușurință de pornire” sunt concepte separate.

## Semnale de vizionare

CineCalendar diferențiază explicit:

- `chosen` — film păstrat/ales;
- `trailer_opened` — interes slab;
- `stremio_opened` — handoff către Stremio, nu dovadă de playback;
- `playback_confirmed` — utilizatorul confirmă că filmul a pornit;
- `watched` — utilizatorul confirmă că l-a văzut;
- `skip_today` — refuz contextual pentru moment.

În schema v5, evenimentele noi sunt legate de expunerea exactă de recomandare prin `exposure_history_id`. Telemetry pentru trust gate este locală și poate compara rezultatele `trusted`, `backfill` și `red_flag` fără a ghici legătura doar după film și zi.

## Catalog și metadate

### IMDb

Importul `ratings.csv` folosește `Const` drept identitate principală și are fallback normalizat titlu/original/an/tip. Dataseturile oficiale IMDb sunt folosite pentru catalogul de bază; aplicația nu face scraping IMDb.

### TMDb opțional

TMDb poate completa overview, keywords, credits și postere. Tokenul API rămâne local și nu este inclus în backupul de profil.

### MovieLens / ALS

Modelul colaborativ este folosit când mapping-ul și numărul de ratinguri permit. Filmele fără acoperire MovieLens rămân eligibile prin motorul personal de conținut.

## SQLite și persistență

Schema curentă este **v5** și este migrată automat la pornire. Tabelele importante includ:

- `movies`, `ratings`, `feedback`, `watchlist`;
- `recommendation_history`, `recommendation_runs`;
- `recommendation_trust_audit`;
- `user_profile`, `settings`, `metadata_cache`, `import_files`.

SQLite folosește WAL, `foreign_keys=ON`, timeout de blocare și indexuri pentru căutările frecvente.

## Backup

Formatul de profil curent este v2. Exportul nu mai copiază întregul catalog IMDb rebuildabil; salvează doar filmele referite de datele utilizatorului, plus ratingurile, feedbackul, watchlist-ul, istoricul recomandărilor, rulările motorului și telemetry trust-gate.

Importul acceptă și backupurile vechi v1. Este merge-safe și evită dublarea feedbackului/istoricului când același backup este importat din nou. Profilul derivat este recalculat după import.

Nu se exportă `tmdb_token` și nici stări tranzitorii precum bootstrap-ul catalogului sau alegerea temporară pentru ziua curentă.

## Updater

Updaterul Windows este implementat pentru bundle-ul `onedir`:

1. citește manifestul stable;
2. descarcă ZIP-ul;
3. verifică SHA-256;
4. extrage într-un staging sigur;
5. păstrează temporar bundle-ul anterior;
6. înlocuiește fișierele fără a atinge `CineCalendarData`;
7. pornește versiunea nouă și așteaptă health-check;
8. face rollback dacă noua versiune nu confirmă pornirea.

## Build Windows

Calea locală și GitHub Actions folosesc aceeași arhitectură `onedir`:

```bat
scripts\build_windows.bat
```

Scriptul rulează testele, benchmark-ul pe catalog mare, construiește `dist\CineCalendar\CineCalendar.exe` + `_internal` și face smoke launch cu un director de date temporar.

Workflow-ul stable rulează aceleași etape esențiale, publică ZIP-ul Windows și actualizează manifestul updaterului numai după succes.

## Teste și audit

```bash
python -m pytest -q
python scripts/benchmark_engine_v3.py
python scripts/audit_watch_success.py "C:\cale\CineCalendarData\data\cinecalendar.db"
```

Auditul Watch Success este descriptiv. Pragurile V16 nu se auto-reglează din câteva clickuri; raportul marchează datele ca suficiente pentru o primă analiză abia după minimum 20 de recomandări auditate și minimum 5 porniri confirmate.

## Limitări

Vezi `docs/LIMITATIONS.md` pentru limitele actuale și condițiile în care anumite semnale nu sunt disponibile.
