CineCalendar Premium — Windows 10/11 x64

PORNIRE
1. Dezarhivează întregul ZIP într-un folder normal cu drept de scriere.
2. Păstrează CineCalendar.exe împreună cu folderul _internal și celelalte fișiere din bundle.
3. Rulează CineCalendar.exe.
4. Importă exportul IMDb ratings.csv. Dacă nu există încă un catalog de filme nevăzute, CineCalendar poate construi automat catalogul din dataseturile oficiale IMDb.

DATE
Datele personale, baza SQLite, cache-ul, logurile, backupurile și fișierele updaterului sunt în folderul CineCalendarData de lângă aplicație. Actualizarea bundle-ului nu șterge CineCalendarData. CineCalendar verifică baza la pornire și păstrează un snapshot last-good pentru recovery; a doua instanță a aplicației este blocată înainte să deschidă baza.

RECOMANDĂRI
Motorul curent combină profilul tău de ratinguri, retrieval ALS/content, contextul calendaristic și semnale locale de vizionare. Din 4.6, raportul ALS/conținut este ales local prin backtest pe istoricul tău și rămâne 70/30 dacă niciun challenger nu dovedește un câștig stabil. Din 3.3, fiecare card vizibil are o expunere imuabilă proprie, iar alegerea, skip-ul, Stremio, confirmarea playback-ului și „L-am văzut” sunt evenimente separate legate de acea expunere exactă.

BACKUP
„Export profile” creează un backup portabil dintr-un singur snapshot SQLite coerent, fără a copia întregul catalog IMDb rebuildabil și fără tokenul TMDb. Importul normal folosește Merge (datele locale mai noi câștigă); Restore este modul autoritar. Importul Merge păstrează informația locală mai nouă; Restore este modul explicit în care backupul devine autoritar.

UPDATE
Actualizările stable se pot instala din aplicație. ZIP-ul este verificat SHA-256, noul bundle este staged, iar updaterul păstrează temporar versiunea veche și un snapshot SQLite. Dacă health-check-ul noii versiuni eșuează, sunt restaurate atât bundle-ul, cât și baza de date.

Sursa catalogului automat: IMDb datasets oficiale pentru uz personal/necomercial. Nu se face scraping IMDb.
