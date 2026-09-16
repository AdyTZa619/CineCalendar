# Limitări curente

- **Prima construcție a catalogului necesită internet.** `title.basics.tsv.gz` și `title.ratings.tsv.gz` sunt descărcate din dataseturile oficiale IMDb dacă nu există deja un catalog local utilizabil.
- **Dataseturile IMDb de bază nu conțin tot.** Pentru overview, keywords, credits și postere bogate este necesar enrichment TMDb sau un catalog local îmbogățit.
- **TMDb necesită token real și branding conform termenilor TMDb.** Fără token, aplicația nu simulează metadate externe.
- **ALS nu acoperă toate filmele.** Titlurile fără mapping MovieLens rămân eligibile prin motorul personal de conținut, dar nu au semnal colaborativ.
- **Watch Success nu este o probabilitate calibrată.** Startability și trust gate sunt semnale conservative de ordonare. Pragurile nu se auto-reglează din câteva interacțiuni.
- **Auditul trusted/backfill are nevoie de rezultate reale.** Pentru o primă analiză, raportul cere minimum 20 de recomandări auditate și minimum 5 porniri confirmate. Până atunci nu este corect să pretindem că un prag nou este superior.
- **Datele istorice 3.1 pot avea unele acțiuni fără legătură explicită la expunere.** 3.2 le atribuie retroactiv numai când există un singur candidat neambiguu film/zi; cazurile ambigue sunt raportate și nu sunt ghicite.
- **Updaterul automat este Windows-only.** Funcționează din bundle-ul PyInstaller `CineCalendar.exe`; rularea directă din Python nu încearcă să se autoînlocuiască.
- **Posterul este disponibil offline numai după cache.**
- **Backupul de profil nu include catalogul IMDb complet.** Acesta este intenționat rebuildabil. Backupul păstrează datele utilizatorului și filmele referite de acestea.
- **Acceptance-ul din `docs/ACCEPTANCE.md` este o fotografie istorică din 13 septembrie 2026.** Valorile de acolo nu trebuie confundate cu schema/testele versiunii curente.
