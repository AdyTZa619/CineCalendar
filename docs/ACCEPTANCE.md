# Acceptance run — 13 septembrie 2026

Fișier real folosit: `ratinguri imdb 05.09.2026.csv` (nu este inclus în distribuția sursă).
Rularea este reproductibilă cu:

```bat
python scripts\acceptance_real.py "C:\cale\ratings.csv" acceptance.json
```

## Rezultate pe exportul real

- rânduri CSV detectate: **2.434**;
- ratings DB: **2.434**;
- movies DB după import: **2.434**;
- candidați nevăzuți furnizați de `ratings.csv`: **0**;
- IMDb IDs duplicate: **0**;
- duplicate ratings pe `movie_id`: **0**;
- schema SQLite: **v4**;
- testele automate: **18/18 trecute**;
- smoke test DB/restart/persistență: **trecut**;
- Paști 2026: **12 aprilie**;
- Înălțarea Domnului 2026: **21 mai**;
- Rusalii 2026: **31 mai**;
- echinocțiul de toamnă calculat: **23 septembrie**;
- grupe 13–30 septembrie generate din repere: **13–15**, **16–22**, **23–26**, **27–30**.

Raportul JSON exact al rulării este livrat separat în `dist_artifacts/acceptance_real_2026-09-13.json`.

## Acceptance pentru recomandările reale

Partea `3 recomandări pentru azi` + `3 pentru fiecare interval al lunii` nu poate fi validată corect numai din `ratings.csv`, deoarece exportul conține tocmai titlurile deja evaluate, care trebuie excluse. În rularea reală rezultă **0 candidați nevăzuți**.

Aplicația nu introduce filme fictive și nu livrează un mini-catalog hardcodat doar pentru a face testul să pară trecut. După importul unui catalog legitim (IMDb datasets oficiale, catalog CSV real sau catalog îmbogățit prin TMDb cu tokenul utilizatorului), același motor execută automat verificările: exclus evaluat, deduplicare, Romance, calendar, profil și explicația scorului.
