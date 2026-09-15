# CineCalendar 0.2.0

Schimbarea principală: utilizatorul NU mai trebuie să adauge sau să descarce manual filme pentru catalog.

Flux normal:
1. Pornești CineCalendar.
2. Importi `ratings.csv`.
3. Dacă nu există candidați nevăzuți, aplicația descarcă automat dataseturile oficiale IMDb `title.basics.tsv.gz` și `title.ratings.tsv.gz`.
4. Fișierele sunt validate și păstrate în cache.
5. Catalogul local este construit automat în SQLite și recomandările devin disponibile.

Detalii tehnice:
- download streaming cu `.part` și reluare prin HTTP Range când este disponibil;
- rename atomic după terminarea downloadului;
- validare gzip + header TSV înainte de import;
- import batch/upsert optimizat pentru cataloage mari;
- actualizare manuală printr-un singur buton „Actualizează catalogul de pe IMDb”;
- importul manual al dataseturilor a rămas doar ca fallback avansat;
- 19 teste automate trec, inclusiv testul nou pentru download/validare/import automat al catalogului.
