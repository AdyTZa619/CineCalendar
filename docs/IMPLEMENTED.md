# Funcții implementate exact

1. Import manual IMDb `ratings.csv` cu detectarea coloanelor după nume.
2. Validare CSV, UTF-8 BOM, diacritice și ordine arbitrară a coloanelor.
3. Hash SHA-256 + size + mtime pentru deduplicarea exporturilor.
4. Detectare rating nou / rating modificat.
5. Rating manual instant și reconciliere ulterioară cu IMDb.
6. Deduplicare primară după IMDb Const; fallback titlu/original/an/tip.
7. SQLite WAL + migrații.
8. Profil pe gen, teme, regizor, deceniu, țară, runtime, popularitate, diferență user–IMDb.
9. Pondere temporală moderată și shrinkage contra outlier-urilor.
10. Semantic tags controlate + vector sparse/cosine.
11. Calendar ortodox fix și mobil; Paște calculat dinamic.
12. Calendar secular/istoric selectiv.
13. Faze sezoniere și puncte de echinocțiu/solstițiu.
14. Calendar relevance: direct / istoric / spiritual / atmosferic / slab.
15. RecommendationEngine transparent cu ponderi configurate în cod.
16. Romance dominant exclus implicit; secundar penalizat.
17. Excludere SQL a filmelor evaluate/văzute/respinse.
18. Penalizare recomandări repetate.
19. Diversity reranking.
20. 3 recomandări pentru pagina Azi.
21. Programul lunii cu câte 3 recomandări pe intervale dinamice.
22. Butoane de feedback cerute.
23. Watchlist.
24. Istoric recomandări.
25. Pagina Profilul meu.
26. Monitorizare automată a folderului cu polling la 30 sec.
27. Catalog CSV.
28. Import oficial IMDb `title.basics.tsv.gz` + `title.ratings.tsv.gz` fără scraping.
29. Provider TMDb cu Bearer token, cache și enrichment.
30. Poster async + cache local.
31. Dark/light.
32. DPI manifest PerMonitorV2.
33. Backup export/import ZIP.
34. Rotating log `logs/CineCalendar.log`.
35. Updater declarat explicit ca dezactivat/neimplementat.
36. Build PyInstaller one-file Windows.
37. Workflow Windows GitHub Actions.
38. Teste automate și smoke test de restart/persistență.
39. Coloane normalizate indexate pentru deduplicare robustă title/original title/an/tip.
40. TMDb enrichment în batch din UI, prioritar pentru titlurile evaluate.
41. Tokenul TMDb este exclus din exportul de profil.
42. Script reproductibil de acceptance pe exportul IMDb real.


## CineCalendar 0.2.0
- Catalog automat IMDb: download direct din `datasets.imdbws.com` pentru `title.basics.tsv.gz` și `title.ratings.tsv.gz`.
- Validare gzip/TSV înainte de import.
- Download cu `.part`, resume când serverul acceptă Range și rename atomic la final.
- Pornire automată a bootstrapului după importul ratings.csv dacă baza nu are candidați nevăzuți.
- Buton de actualizare automată a catalogului; importul manual rămâne doar fallback.
- Import optimizat în batch pentru cataloage mari.
