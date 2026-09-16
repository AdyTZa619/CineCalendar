CineCalendar Premium — Windows 10/11 x64

PORNIRE
1. Dezarhivează întregul ZIP într-un folder normal cu drept de scriere.
2. Păstrează CineCalendar.exe împreună cu folderul _internal și celelalte fișiere din bundle.
3. Rulează CineCalendar.exe.
4. Importă exportul IMDb ratings.csv. Dacă nu există încă un catalog de filme nevăzute, CineCalendar poate construi automat catalogul din dataseturile oficiale IMDb.

DATE
Datele personale, baza SQLite, cache-ul, logurile, backupurile și fișierele updaterului sunt în folderul CineCalendarData de lângă aplicație. Actualizarea bundle-ului nu șterge CineCalendarData.

RECOMANDĂRI
Motorul curent combină profilul tău de ratinguri, retrieval ALS/content, contextul calendaristic și semnale locale de vizionare. Deschiderea Stremio nu este tratată ca dovadă că filmul a pornit; confirmarea playback-ului și „L-am văzut” sunt semnale distincte.

BACKUP
„Export profile” creează un backup portabil al datelor tale relevante fără a copia întregul catalog IMDb rebuildabil și fără tokenul TMDb.

UPDATE
Actualizările stable se pot instala din aplicație. ZIP-ul este verificat SHA-256, noul bundle este staged, iar updaterul păstrează temporar versiunea veche pentru rollback până trece health-check-ul.

Sursa catalogului automat: IMDb datasets oficiale pentru uz personal/necomercial. Nu se face scraping IMDb.
