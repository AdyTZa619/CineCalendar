# Limitări curente

- **Prima construcție a catalogului necesită internet.** Dataseturile oficiale IMDb sunt descărcate dacă nu există un catalog local utilizabil.
- **IMDb datasets nu conțin toate metadatele bogate.** Overview, keywords, credits și postere pot necesita TMDb sau cache local.
- **TMDb necesită token real și respectarea termenilor/brandingului TMDb.** Tokenul rămâne local și nu intră în backup.
- **ALS nu acoperă toate filmele.** Titlurile fără mapping MovieLens rămân eligibile prin motorul personal de conținut.
- **Watch Success și Startability nu sunt probabilități calibrate.** Sunt semnale bounded; pragurile nu trebuie auto-reglate din puține interacțiuni.
- **Auditul pentru tuning cere date reale suficiente.** Minimum orientativ: 20 de expuneri auditate și 5 porniri confirmate; mai mult este preferabil.
- **Datele istorice 3.1/3.2 pot conține evenimente fără `exposure_history_id`.** Sunt atribuite retroactiv numai când există o singură expunere neambiguă; cazurile ambigue/orfane sunt raportate și nu sunt ghicite.
- **Updaterul automat este Windows-only.** Bundle-ul și snapshotul SQLite au rollback comun, dar sursa Python nu se autoînlocuiește.
- **Manifestul Stable este verificat prin HTTPS + SHA-256, nu printr-o semnătură public-key separată.** Semnarea codului/manifestului rămâne o întărire de supply-chain posibilă.
- **Posterul este offline numai după cache.**
- **Backupul de profil nu include catalogul IMDb complet.** Catalogul este rebuildabil; backupul păstrează starea utilizatorului și filmele referite.
- **`merge` și `restore` au intenții diferite.** `merge` păstrează valorile locale mai noi pentru ratinguri/setări/watchlist; `restore` face backupul autoritar pentru aceste stări.
- **Acceptance-ul din `docs/ACCEPTANCE.md` este o fotografie istorică.** Nu trebuie confundat cu testele sau schema versiunii curente.

- **Recovery-ul automat poate reveni la ultimul snapshot `last_good`.** Dacă baza principală se corupe după modificări recente, recuperarea privilegiază consistența bazei; backupul de profil rămâne mecanismul pentru portabilitatea stării utilizatorului.
