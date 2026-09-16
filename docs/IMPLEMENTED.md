# Funcții implementate

## Date și catalog

- import IMDb `ratings.csv` cu detectarea coloanelor după nume, UTF-8 BOM și ordine arbitrară;
- identitate primară IMDb Const, cu fallback normalizat titlu/original/an/tip;
- detectare rating nou/modificat și reconciliere cu ratingurile introduse manual;
- catalog automat din dataseturile oficiale IMDb, cu download `.part`, validare și import streaming/batch;
- catalog CSV local și enrichment TMDb opțional;
- postere asincrone cu cache local.

## Recomandări

- motor de producție V16;
- retrieval personal pe întregul catalog prin ALS + vecini ai favoritelor + discovery generic când mapping-ul permite;
- fallback content pentru titluri fără acoperire MovieLens;
- profil personal construit din ratinguri 1–10, features semantice și feedback;
- Adaptive Personal v2 cu holdout temporal și influență plafonată;
- calendar ortodox fix/mobil, calendar secular/istoric selectiv și sezonalitate;
- filtrare explicită de gen pentru ziua curentă; nu există un veto global permanent pe Romance;
- repetare penalizată, diversitate finală și excluderea filmelor evaluate/văzute/respinse;
- Watch Success v3: `chosen`, trailer, Stremio handoff, playback confirmat, watched și skip;
- Startability folosit numai pentru departajarea candidaților apropiați;
- Top-3 trust gate V16 cu semnale multiple, red flags și backfill conservator;
- telemetry locală `trusted/backfill/red_flag`, fără reglare automată a pragurilor.

## UI

- „Ce văd acum?”, recomandări, profil, ratinguri, watchlist, calendar, program lunar, istoric, actualizări și setări;
- dark/light, DPI PerMonitorV2, carduri cu poster și explicații;
- deschidere Stremio/Stremio Web, trailer și confirmare explicită că filmul a pornit;
- pagină dedicată cinematografiei românești și filtru de gen pentru ziua curentă.

## Persistență și siguranță

- SQLite WAL cu migrații automate; schema curentă v5;
- evenimentele de interacțiune sunt legate de expunerea exactă prin `exposure_history_id`;
- feedbackul `seen` nu suprascrie evenimentul `watched`;
- backup profil v2: ratinguri, feedback, watchlist, istoric, recommendation runs și trust telemetry, fără întregul catalog rebuildabil;
- import backup v1/v2 merge-safe și idempotent pentru evenimentele restaurate;
- tokenul TMDb și stările tranzitorii nu intră în backup;
- log rotativ local.

## Update și distribuție

- updater stable Windows pentru bundle `onedir`, cu SHA-256, staging, backup temporar, health-check și rollback;
- build local și GitHub Actions aliniate pe aceeași arhitectură `onedir`;
- teste automate, benchmark pe catalog mare, build Windows și smoke launch înainte de publicarea stable.
