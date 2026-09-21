# Funcții implementate

## Date și catalog
- import IMDb `ratings.csv` cu identitate primară IMDb Const și fallback normalizat;
- detectare rating nou/modificat și reconciliere cu ratingurile locale;
- catalog automat din dataseturile oficiale IMDb, validare și import streaming/batch;
- enrichment TMDb opțional și postere asincrone cu cache local.

## Recomandări
- baseline de gust V16/V17 ales local prin backtest;
- retrieval ALS + vecini ai favoritelor + content/discovery fallback;
- profil personal din ratinguri 1–10, semantică și feedback;
- Adaptive Personal v2 cu validare temporală și influență plafonată;
- calendar ortodox/secular/istoric/sezonier;
- Watch Success + Startability + Top-3 trust/red-flag gate;
- în 3.3, Watch Success învață și auditează la nivel de expunere concretă, nu `movie_id + zi`;
- în 3.6, retrieval local suplimentar din semnale repetate de regizor și țară+gen, cu ratingurile 1–4 ca protecție negativă;
- în 3.7, challengerul este construit peste exact baseline-ul V16/V17 aprobat pentru utilizator, iar procentul lane-ului local este calibrat dintre 8%, 14% și 20%;
- în 4.6, raportul ALS/conținut nu mai este identic pentru toți utilizatorii: 50/50, 60/40 și 80/20 concurează cu baseline-ul 70/30 pe istoricul local;
- o pondere 4.6 este activată numai dacă trece ferestre temporale ne-suprapuse, recall 8+/9+, NDCG și protecția contra recomandărilor slab evaluate;
- evaluarea 3.7 folosește ferestre temporale ne-suprapuse și elimină din training atât holdout-ul curent, cât și toate ratingurile ulterioare;
- Availability Guard 3.7 elimină titlurile cu an sau dată de lansare cunoscută după data recomandării, fără să schimbe ordinea filmelor eligibile;
- Context Intelligence păstrează acum exact motorul aprobat, inclusiv V18/V19, în loc să îl reducă la o clasă V16/V17.

## UI și acțiuni
- Home, recomandări, profil, ratinguri, watchlist, calendar, program lunar, istoric, update și setări;
- Stremio/Stremio Web, trailer, confirmare explicită playback și watched;
- fiecare recomandare vizibilă primește un `exposure_history_id` care este transportat până la acțiunea utilizatorului;
- două expuneri ale aceluiași film în aceeași zi rămân două funnel-uri distincte;
- Taste Hub afișează diagnosticul motorului și lanțul de calibrări personale fără a permite UI-ului să forțeze un challenger nevalidat.
- feedbackul 4.4 separă explicit „Ascunde doar filmul” de „Nu-mi recomanda similare”;
- „Ascunde doar filmul” exclude titlul exact fără să antreneze profilul, ALS, Adaptive Personal sau Watch Success împotriva caracteristicilor lui;
- feedbackul aplicat în sesiunea curentă poate fi anulat exact, în ordine inversă, din bara laterală sau cu `Ctrl+Z`; Watchlist-ul derivat este refăcut tranzacțional.

## Persistență și siguranță
- SQLite WAL, schema curentă v9, `quick_check` la startup și snapshot automat `last_good` pentru recovery;
- expunerile de recomandare sunt rădăcini imuabile; `chosen`, `skip_today`, `trailer_opened`, `stremio_opened`, `playback_confirmed` și `watched` sunt evenimente append-only;
- feedbackul nu mai modifică expunerea originală;
- migrațiile sunt idempotente, verificate cu `foreign_key_check`/`quick_check` și precedate de snapshot SQLite când este necesară o schimbare/reparație;
- backup profil v4 compact, exportat dintr-un singur snapshot WAL consistent;
- import `merge` păstrează datele locale mai noi, iar `restore` este explicit autoritar;
- tokenul TMDb și stările tranzitorii nu intră în backup.

## Update și distribuție
- updater stable Windows `onedir`, SHA-256, staging, health-check și rollback;
- din 3.3 updaterul păstrează și snapshot SQLite și îl restaurează împreună cu bundle-ul la health-check eșuat;
- build local și GitHub Actions folosesc aceeași arhitectură `onedir`;
- single-instance Windows blochează a doua instanță înainte de SQLite/workeri;
- compoziția UI de producție are o singură ordine canonică verificată;
- testele, benchmark-ul, build-ul și smoke launch rulează înainte de publicarea stable.
