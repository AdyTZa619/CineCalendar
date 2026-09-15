# CineCalendar

## 0.2.0 — catalog automat

Fluxul normal nu mai cere utilizatorului să descarce sau să adauge manual filme. După importul `ratings.csv`, dacă nu există candidați nevăzuți, CineCalendar descarcă automat `title.basics.tsv.gz` și `title.ratings.tsv.gz` din endpointul oficial IMDb datasets, validează fișierele, le păstrează în cache și construiește catalogul SQLite. Descărcarea folosește fișier `.part`, poate relua un transfer întrerupt și face rename atomic doar după terminare.

Importul manual al dataseturilor rămâne în Setări doar ca fallback avansat. `Actualizează catalogul de pe IMDb` forțează redescărcarea copiilor oficiale.



CineCalendar este o aplicație Windows portabilă pentru recomandări personalizate de filme, construită în jurul ratingurilor reale ale utilizatorului, al unui calendar cinematografic ortodox/secular/sezonier și al unui motor de scoring explicabil.

## Stare

Versiune sursă: **0.1.0**.

Nucleul funcțional este implementat și testat: SQLite cu migrații, import IMDb `ratings.csv`, rating manual, reconciliere/duplicate, profil personal ponderat temporal, CalendarEngine, RecommendationEngine, istoric, feedback, watchlist, backup, monitorizare folder, catalog CSV, import al dataset-urilor IMDb oficiale și provider TMDb opțional cu enrichment în batch.

Build-ul Windows este definit prin `scripts/build_windows.bat` și `.github/workflows/windows-build.yml`. Mediul în care a fost generat acest proiect este Linux și nu conține toolchain Windows/PyInstaller pentru a produce un `.exe` Windows verificabil local; de aceea repository-ul nu conține un executabil pretins ca testat.

## Principii implementate

- **Gust personal > calendar.** Ponderea gustului + similarității semantice este 58%; calendarul are 14%.
- Ratingurile recente pot cântări cu cel mult aproximativ 15% mai mult decât cele vechi.
- Un singur rating extrem este regularizat prin shrinkage pe fiecare feature.
- `6/10` produce doar un semnal ușor pozitiv, nu este tratat ca apreciere puternică.
- Titlurile evaluate sunt excluse din candidați la nivel SQL.
- `Romance` este exclus implicit când este dominant. Romance secundar poate trece numai cu un motiv non-romantic puternic și primește penalizare.
- Recomandările recente primesc penalizare; respingerile explicite și `Am văzut` suprimă titlul.
- Feedbackul are ponderi moderate și nu poate suprascrie brutal istoricul de ratinguri.

## Scoring

Formula de bază:

- 36% gust personal;
- 22% similaritate semantică;
- 14% relevanță calendaristică;
- 8% potrivire sezonieră;
- 7% regizor/cinematografie;
- 5% noutate;
- 3% diversitate;
- 5% calitate publică regularizată după numărul de voturi.

Se aplică separat penalizări pentru repetare și Romance secundar.

`De ce mi-ai recomandat asta?` afișează contribuțiile pozitive și negative în puncte procentuale aproximative.

## CalendarEngine

Include sărbători ortodoxe fixe și mobile, perioade religioase, o selecție limitată de zile seculare/istorice cu relevanță cinematografică și faze sezoniere.

Paștele ortodox este calculat pentru anul cerut folosind Pascalionul iulian și conversie generală la calendarul gregorian. Pentru 2026 rezultatul testat este 12 aprilie. Sărbătorile mobile precum Floriile, Înălțarea și Rusaliile sunt derivate din această dată.

Relevanța calendaristică diferențiază:

- directă;
- istorică;
- spirituală;
- atmosferică;
- slabă.

Exemplu de regulă implementată: un film generic despre Patimile lui Hristos nu este considerat automat `direct` pentru Înălțarea Sfintei Cruci; pentru relevanță directă acolo sunt necesare semnale specifice legate de Sfânta Cruce.

## Surse de metadate

### 1. CSV local

`Setări -> Import catalog CSV` acceptă coloane precum:

`imdb_id,title,original_title,year,title_type,runtime,genres,directors,countries,overview,keywords,imdb_rating,num_votes,release_date,poster_url`

Ordinea coloanelor nu contează.

### 2. IMDb datasets oficiale

`Setări -> Import IMDb title.basics + title.ratings` citește fișierele `.tsv.gz` descărcate de utilizator. Nu se face scraping IMDb.

IMDb precizează că pentru utilizare personală/necomercială datele trebuie luate din dataset-urile puse la dispoziție și interzice screen scraping-ul. Vezi: https://help.imdb.com/article/imdb/general-information/can-i-use-imdb-data-in-my-software/G5JTRESSHJBBHTGX

Attribution:

> Information courtesy of IMDb (https://www.imdb.com). Used with permission.

### 3. TMDb opțional

`cinecalendar/tmdb.py` implementează autentificarea cu Bearer `API Read Access Token`, `/find/{imdb_id}`, detalii, credits, keywords și cache local. Din UI poate îmbogăți în batch câte 100 de titluri, prioritar cele evaluate, apoi recalculează profilul. Fără token nu este simulată nicio conexiune. Tokenul rămâne local și este exclus din `Export profile`.

Documentație oficială: https://developer.themoviedb.org/docs/authentication-application

TMDb impune attribution și logo oficial aprobat în aplicațiile care folosesc API-ul. Textul obligatoriu este afișat în Setări; logo-ul oficial nu este inclus în acest pachet sursă și trebuie adăugat înaintea unei distribuții cu integrarea TMDb activată. Vezi: https://developer.themoviedb.org/docs/faq

## SQLite și persistență

DB implicit: `CineCalendarData/data/cinecalendar.db` lângă executabil.

Tabele principale:

- `movies`
- `ratings`
- `user_profile`
- `recommendation_history`
- `recommendation_runs`
- `feedback`
- `watchlist`
- `metadata_cache`
- `settings`
- `calendar_events`
- `import_files`
- `schema_migrations`

SQLite rulează cu WAL, `foreign_keys=ON`, `busy_timeout` și migrații versionate.

## Import IMDb ratings.csv

Importerul:

- detectează coloanele după nume/alias, nu după poziție;
- suportă UTF-8 BOM și diacritice;
- validează antetul;
- folosește `Const` ca identitate principală;
- fallback: titlu + original title + an + tip, cu `title_norm` / `original_title_norm` indexate în schema v4;
- nu confundă remake-urile din ani diferiți;
- identifică hash-ul fișierului și nu reimportă același export;
- detectează rating nou și rating modificat;
- reconciliază ratingurile manuale cu IMDb fără duplicare.

## Detectare automată

Monitorizarea folderului este prin polling la 30 secunde, fără dependență fragilă de hooks de filesystem. Sunt comparate numele, dimensiunea, `mtime_ns` și hash-ul înainte de import.

## UI

Secțiuni implementate:

- Azi;
- Calendar;
- Programul lunii;
- Profilul meu;
- Ratinguri IMDb;
- Watchlist;
- Istoric recomandări;
- Setări.

UI-ul are dark/light, sidebar, carduri, scrolling, DPI manifest PerMonitorV2, poster cu încărcare asincronă și cache local, plus placeholder când imaginea nu există.

## Backup

`Export profile` produce ZIP cu JSON pentru filme, ratinguri locale, feedback, setări, istoric, watchlist și profil. `Import profile` face merge pe IMDb ID / identity key.

## Logging

`CineCalendarData/logs/CineCalendar.log`, rotație 2 MB × 5 fișiere. Tokenul TMDb nu este scris în log.

## Build Windows

Pe Windows 10/11 x64 cu Python 3.12:

```bat
scripts\build_windows.bat
```

Scriptul:

1. creează `.venv`;
2. instalează dependențele;
3. rulează testele;
4. produce `dist\CineCalendar.exe` cu PyInstaller `--onefile --windowed`;
5. pornește EXE-ul pentru un smoke check și îl închide după câteva secunde.

Workflow-ul GitHub Actions face aceleași verificări pe `windows-latest` și publică `CineCalendar_Portable_win64.zip` ca artifact.

## Teste

```bash
python -m pytest -q
python scripts/smoke_test.py
```

În rularea finală sunt **18/18 teste trecute**. Acoperirea include import, duplicate, rating modificat, diacritice, coloană reordonată, CSV invalid, reconciliere manuală inclusiv când `Original Title` diferă, Paști, sărbători mobile, relevanță pentru 14 septembrie, echinocțiu, Romance, excluderea filmelor văzute, feedback, repetări, backup fără tokenul TMDb și persistență la restart.

## Acceptance pe exportul real

În mediul de dezvoltare a fost folosit exportul `ratinguri imdb 05.09.2026.csv`:

- 2.434 rânduri importate;
- 2.434 ratinguri persistate;
- 0 IMDb IDs duplicate;
- 0 ratinguri duplicate pe același `movie_id`;
- Paști 2026: 12.04.2026;
- echinocțiul de toamnă 2026: 23.09.2026;
- pentru 13.09.2026 CalendarEngine detectează apropierea Înălțării Sfintei Cruci;
- gruparea 13–30 septembrie 2026 este generată dinamic: 13–15, 16–22, 23–26, 27–30.

Generarea celor 3 recomandări reale necesită și un catalog de titluri **nevăzute**. Exportul `ratings.csv` conține numai titluri deja evaluate, deci aplicația nu inventează candidați și nu tratează o listă hardcodată de 20–50 de filme ca bază de date.

## Limitări curente

Vezi `docs/LIMITATIONS.md`.
