# CineCalendar

## 4.11.0 — Metadata Doctor și coadă persistentă

- candidații probabili sunt puși într-o coadă SQLite înainte de verificarea limitată a listei;
- recomandările vizibile, filmele evaluate și watchlistul au prioritate;
- completarea continuă în loturi mici la pornire și la fiecare 15 minute;
- fiecare film păstrează starea, câmpurile lipsă, încercările și ultima eroare;
- retry-ul crește gradual de la 15 minute la 7 zile;
- un poster care eșuează real la încărcare este invalidat, raportat și pus din nou în coadă;
- pagina **Metadata Doctor** arată coada și permite o reparare manuală imediată;
- formula recomandărilor și ponderile ALS/conținut nu sunt schimbate.

CineCalendar este o aplicație Windows portabilă pentru recomandări personale de filme. Istoricul real de ratinguri rămâne sursa principală de adevăr pentru gust; calendarul ortodox/secular/sezonier, ALS, modelul adaptiv și semnalele de vizionare rafinează selecția fără să înlocuiască profilul personal.

## Flux normal

1. Pornești `CineCalendar.exe` din folderul portabil.
2. Importi exportul IMDb `ratings.csv`.
3. Dacă baza locală nu are încă suficiente filme nevăzute, aplicația construiește catalogul din dataseturile oficiale IMDb.
4. „Ce văd acum?” produce o alegere principală și alternative scurte, iar „Recomandări” oferă o listă mai largă.

Datele sunt păstrate în `CineCalendarData` lângă bundle. Update-ul nu șterge acest director.

## Feedback protejat 4.4

„Nu acum”, „Prea lung pentru moment” și motivele contextuale rămân semnale temporare și nu
rescriu gustul permanent. „Ascunde doar filmul” exclude numai titlul ales; numai acțiunile
explicite „Mai puține ca acesta” și „Nu-mi recomanda similare” pot învăța o preferință negativă
pentru caracteristicile comune. Ultimele acțiuni de feedback din sesiune pot fi anulate în ordine
inversă din bara laterală sau cu `Ctrl+Z`, inclusiv cu refacerea stării Watchlist.

## Motorul curent

CineCalendar nu presupune că motorul cu numărul cel mai mare este automat mai bun. V16/V17 formează baseline-ul de gust măsurat local, iar versiunile ulterioare pot intra în producție numai dacă propriul istoric al utilizatorului dovedește îmbunătățirea.

În 3.7, serviciul compune motorul selectat local cu:

- retrieval personal ALS + vecini ai favoritelor + discovery;
- profil personal pe termen lung din ratingurile 1–10;
- Adaptive Personal v2 cu validare temporală și influență plafonată;
- Watch Success v3.3 la nivel de expunere concretă;
- Startability ca departajare limitată între candidați deja competitivi;
- Top-3 trust/red-flag gate;
- Context Intelligence 3.5 ca strat bounded peste motorul aprobat;
- Availability Guard 3.7, care elimină filmele cu dată/an de lansare cunoscut în viitor fără să reordoneze filmele eligibile;
- calibrare 3.7 a lane-ului local de conținut la 8%, 14% sau 20%, numai dacă acesta bate baseline-ul pe ferestre temporale independente.
- calibrare personală 4.6 a raportului ALS/conținut: baseline 70/30 versus challengeri 50/50, 60/40 și 80/20, aleși exclusiv prin istoricul local al utilizatorului.

Evaluarea 3.7 folosește ferestre de holdout ne-suprapuse. Pentru o fereastră istorică, ratingurile din acea fereastră și toate ratingurile ulterioare sunt eliminate din copia de training, iar baseline-ul și challengerul sunt evaluate cu aceeași regulă de disponibilitate a filmelor. Dacă niciun procent nu trece toate gardurile de recall 8+/9+, NDCG, expunere a filmelor slab notate și scor compozit, baseline-ul validat rămâne activ.

În 4.6, aceeași disciplină se aplică ponderii dintre colaborarea MovieLens și profilul personal de conținut. O combinație nouă devine activă doar după minimum 170 de ratinguri, câștig stabil pe ferestre temporale independente și lipsa regresiilor materiale la filmele de 8+/9+ sau la expunerea celor evaluate slab. Verdictul se aplică la pornirea următoare; recomandarea curentă nu așteaptă backtestul.

În 4.7, verdictul nu mai este invalidat de feedback contextual precum „Nu acum”. După primul
backtest, recalibrarea așteaptă un lot relevant de ratinguri noi sau modificate (1% din bibliotecă,
cu minimum 12 și maximum 40), iar ultima formulă validată rămâne activă. Fiecare formulă are o
identitate separată în outcome telemetry. După activare, o gardă live compară alegerile,
vizionările, ratingurile de minimum 8 și eroarea estimării cu referința anterioară. Rollback-ul la
motorul sigur cere minimum două regresii independente și suficient eșantion; un singur rezultat
slab nu poate retrage formula. Cele patru formule împart aceeași copie SQLite pentru fiecare
fereastră temporală, reducând numărul copiilor complete de la patru la una per fereastră.

În 4.8, pagina Recomandări a introdus verificarea în fundal a titlurilor vizibile folosind
aceleași surse și același cache care înainte completau cardurile numai după clasare. Dacă sunt
adăugate genuri, regizori, țări, synopsis/keywords sau durată lipsă, motorul face o singură
reclasare cu faptele noi. Un poster nou sau o informație exclusiv vizuală nu schimbă ordinea.
Cardul „Datele recomandărilor” afișează acoperirea reală, numărul de titluri complete și dacă
reclasarea a fost justificată. Din 4.9.3, toate cele 12 carduri vizibile au prioritate la completare;
backtestul 4.7 nu este invalidat de această operațiune.

În 4.8.1, expunerile create de interfață păstrează identitatea completă a formulei ALS/conținut,
astfel încât protecția live măsoară exact formula care a produs recomandarea. Pagina nu mai
înregistrează lista provizorie înaintea reclasării: preflight-ul se termină înainte de afișarea
listei finale, iar o deschidere produce un singur set de expuneri. Erorile surselor sunt separate
în stări complete, parțiale și eșuate și sunt afișate explicit.

În 4.9, preflight-ul alege verificările cu impact dintre 36 de finaliști și respectă
un buget total de timp, astfel încât o sursă lentă nu ține blocată pagina. TMDb selectează postere
w500 cu prioritate română/engleză, iar Wikidata/Wikipedia rămâne fallback fără cheie și cu cache.
Lista finală are 12 filme, fiecare card permite alegerea directă, iar protecția live se reîmprospătează
după alegere, vizionare sau rating. Backtesturile verifică spațiul liber înainte de orice copie SQLite.

În 4.9.1, Home afișează o singură estimare finală și separă vizual potrivirea, ușurința de pornire
și rezerva de intenție. TMDb cere descrierea în română, folosește engleza doar ca fallback și arată
sursa descrierii/posterului în Detalii. Un token validat este salvat și activat imediat, fără restart.
Localizarea nu schimbă formula de ranking și nu declanșează backtestul.

În 4.9.2, „L-am văzut” nu cere o notă duplicată în aplicație. Filmul intră într-o coadă locală
„aștept nota IMDb”, iar profilul public este reverificat după aproximativ 2 și 10 minute, apoi prin
sincronizarea normală la 30 de minute sau la următoarea pornire. Când nota reală apare pe IMDb,
CineCalendar o importă, reconstruiește profilul și confirmă vizibil ratingul folosit.

În 4.9.3, completarea TMDb prioritizează toate cele 12 recomandări afișate, astfel încât posterele
și metadatele disponibile apar chiar în cardurile pe care le vezi. Cardurile își ajustează înălțimea
după explicație, acțiunile sunt așezate compact, iar lista trece automat pe o singură coloană când
fereastra este prea îngustă.

În 4.9.4, fiecare card incomplet explică dacă filmul nu a fost găsit, sursa a răspuns cu date
parțiale, a apărut o eroare temporară sau s-a atins limita de timp. Butonul „Reîncearcă doar
lipsurile” șterge exclusiv cache-ul furnizorilor pentru filmele incomplete vizibile și le verifică
din nou, fără să atingă metadatele deja confirmate sau formula de ranking.

În 4.10, urmărirea notei IMDb este stocată într-o coadă SQLite auditată. Fiecare film păstrează
starea, numărul încercărilor, ultima și următoarea verificare, eroarea individuală și IMDb ID-ul
care a confirmat nota. Reverificările folosesc backoff până la 24 de ore, iar auditul recuperează
automat filmele marcate văzute care au rămas fără rating și fără intrare în coadă.

Nota „pentru tine”, încrederea, Startability, contextul și trust gate sunt semnale distincte.

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

Schema curentă este **v9**. În 3.3 s-a schimbat mecanismul de aplicare a migrărilor:

- pașii cu `ALTER TABLE` sunt verificați înainte de aplicare;
- migrarea poate relua în siguranță o schemă parțial modificată;
- versiunea migrării este înscrisă în aceeași tranzacție cu modificarea;
- înaintea unei migrări necesare se face snapshot SQLite consistent;
- după migrare se rulează `foreign_key_check` și `quick_check`;
- dacă migrarea eșuează, snapshotul local este restaurat înainte ca eroarea să fie propagată.

## Backup profil

Formatul de profil este v4 și nu copiază întregul catalog IMDb rebuildabil. Include filmele referite de starea utilizatorului, ratingurile, feedbackul, watchlist-ul, recommendation history/runs, outcomes, explicațiile salvate și trust telemetry.

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

Pragurile de recomandare nu sunt auto-reglate din câteva clickuri. Calibrarea motorului se face numai cu suficient istoric și cu backtesturi temporale locale, iar protecția live cere două semnale independente înainte de rollback.

## Limitări

Vezi `docs/LIMITATIONS.md`.


### Fundație 3.3
CineCalendar 3.3 adaugă identitate exactă a expunerilor, evenimente append-only, audit pe expunere, migrații idempotente, quick-check/recovery SQLite, backup WAL consistent, rollback comun bundle+DB, single-instance Windows și compoziție UI canonică verificată.
