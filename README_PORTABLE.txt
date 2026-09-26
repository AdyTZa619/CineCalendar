CineCalendar Premium 4.14.1 — Windows 10/11 x64

NOU ÎN 4.14.1 — REPLAY ISTORIC REPARAT
- Historical Replay poate crea și curăța corect workspace-ul temporar;
- nu schimbă recomandările, formula 70/30 sau rezultatele evaluatorului 4.14.

NOU ÎN 4.14.0 — FULL-CATALOG EVALUATION
- combinațiile de gust deja învățate pot genera direct candidați în challengerul shadow;
- rezultatele shadow live sunt comparate numai în ferestre temporale finite și sunt marcate ca observaționale;
- challengerul ascuns nu poate fi promovat doar pentru că a strâns suficiente ratinguri;
- replay-ul temporal offline poate compara baseline-ul cu o injecție conservatoare full-catalog fără leakage din ratingurile viitoare;
- recomandările afișate și formula activă rămân neschimbate până la dovezi istorice stabile.

NOU ÎN 4.13.0 — FULL-CATALOG SHADOW RETRIEVAL
- caută separat filmele fără mapping MovieLens/ALS folosind gustul învățat din ratingurile tale;
- verifică genuri, regizori, țări, teme, decade, durată și combinații;
- nu schimbă recomandările afișate și nu modifică formula 70/30;
- salvează baseline-ul și challengerul pentru comparație ulterioară pe ratinguri IMDb reale;
- dashboardul arată progresul, iar promovarea rămâne blocată până la suficiente rezultate și analiză offline.

NOU ÎN 4.12.0 — RELIABILITY GATE
- procentul euristic este etichetat corect „dovezi personale”, nu probabilitate de succes;
- recomandările afișează intervalul estimat al notei;
- verdictul „Recomandare verificată” apare numai după minimum 30 rezultate reale și praguri clare de precizie;
- pagina Recomandări arată eroarea medie și procentul estimărilor aflate la maximum un punct de nota IMDb;
- formula 70/30 și ordinea recomandărilor nu sunt schimbate.

PORNIRE
1. Dezarhivează întregul ZIP într-un folder normal cu drept de scriere.
2. Păstrează CineCalendar.exe împreună cu folderul _internal și celelalte fișiere din bundle.
3. Rulează CineCalendar.exe.
4. Importă exportul IMDb ratings.csv. Dacă nu există încă un catalog de filme nevăzute, CineCalendar poate construi automat catalogul din dataseturile oficiale IMDb.

DATE
Datele personale, baza SQLite, cache-ul, logurile, backupurile și fișierele updaterului sunt în folderul CineCalendarData de lângă aplicație. Actualizarea bundle-ului nu șterge CineCalendarData. CineCalendar verifică baza la pornire și păstrează un snapshot last-good pentru recovery; a doua instanță a aplicației este blocată înainte să deschidă baza.

METADATA DOCTOR
Coada persistentă completează genuri, regizori, țară, descriere, durată și poster.
Recomandările vizibile au prioritate; erorile primesc retry gradual, iar posterele
care eșuează efectiv la încărcare sunt invalidate și căutate din nou.

RECOMANDĂRI
Motorul curent combină profilul tău de ratinguri, retrieval ALS/content, contextul calendaristic și semnale locale de vizionare. Din 4.6, raportul ALS/conținut este ales local prin backtest pe istoricul tău și rămâne 70/30 dacă niciun challenger nu dovedește un câștig stabil. Din 4.7, formula activă este verificată pe rezultate reale cu rollback conservator. Din 4.8.1, lista finală produce un singur set de expuneri și păstrează identitatea completă 70/30–80/20. În 4.9, Recomandări verifică ținte cu impact dintre 36 de finaliști, are buget total de timp, postere TMDb w500 cu fallback Wikimedia și permite alegerea directă din card; protecția se actualizează imediat după acțiunile reale. În 4.9.1, explicația arată un singur scor final, separă potrivirea de ușurința de pornire, cere descrieri TMDb în română și activează imediat tokenul validat. În 4.9.2, filmele marcate văzute așteaptă nota reală din profilul IMDb și o importă automat, fără introducerea aceleiași note în aplicație. În 4.9.3, toate cele 12 recomandări vizibile au prioritate la completarea TMDb, iar cardurile se adaptează fără text tăiat sau scroll orizontal. În 4.9.4, fiecare lipsă are un diagnostic vizibil, iar „Reîncearcă doar lipsurile” reverifică numai filmele incomplete și elimină cache-ul gol aferent. În 4.10, coada ratingurilor IMDb păstrează încercările, backofful, erorile și identitatea exactă pentru fiecare film și recuperează automat intrările lipsă. Din 3.3, fiecare card vizibil are o expunere imuabilă proprie, iar alegerea, skip-ul, Stremio, confirmarea playback-ului și „L-am văzut” sunt evenimente separate legate de acea expunere exactă.

BACKUP
„Export profile” creează un backup portabil dintr-un singur snapshot SQLite coerent, fără a copia întregul catalog IMDb rebuildabil și fără tokenul TMDb. Importul normal folosește Merge (datele locale mai noi câștigă); Restore este modul autoritar. Importul Merge păstrează informația locală mai nouă; Restore este modul explicit în care backupul devine autoritar.

UPDATE
Actualizările stable se pot instala din aplicație. ZIP-ul este verificat SHA-256, noul bundle este staged, iar updaterul păstrează temporar versiunea veche și un snapshot SQLite. Dacă health-check-ul noii versiuni eșuează, sunt restaurate atât bundle-ul, cât și baza de date.

Sursa catalogului automat: IMDb datasets oficiale pentru uz personal/necomercial. Nu se face scraping IMDb.
