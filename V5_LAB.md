# CineCalendar 5.0 Lab

CineCalendar 5.0 is developed in parallel with the 4.14.x stable line. Alpha code is not wired into production recommendations.

## Product contract

Given the unseen catalog, choose films the user is most likely to rate highly now, preserve the ability to abstain when evidence is weak, and never promote a new ranking component without temporal evidence.

## Alpha 1 architecture

The V5 core is composition-first:

1. **Unified retrieval** keeps the proven baseline pool intact and appends a bounded set of novel candidates.
2. Candidate evidence is fused across ALS, favourite-neighbour, local-content and full-catalog lanes with reciprocal-rank fusion and explicit source attribution.
3. **Personal utility ranker** trains two local models on timestamped ratings: likelihood signal for rating >= 8 and risk signal for rating <= 4.
4. The utility ranker has its own temporal validation gate. Its score is not exposed as a calibrated probability.
5. **V5 Lab adapter** exists only to evaluate the new components with the existing backtest framework. Alpha 1 enables retrieval only; the utility ranker remains observational until an integration strategy passes replay.

## Non-negotiable promotion gate

V5 must beat the current V16 baseline on the same temporal windows. Promotion requires:

- no regression in candidate recall for 8+ and 9+;
- no material increase in exposure to <=4 outcomes;
- NDCG improvement or at minimum no regression across the promotion windows;
- a positive aggregate quality gain across the majority of windows;
- acceptable runtime on the 260k-catalog benchmark.

A component that looks promising on its own validation split but fails end-to-end replay remains in Lab.


## Data foundation added in alpha1

A real blocker showed up in the user's production database: the rating history has strong identity,
genre and director coverage, but rich premise/country metadata is sparse. V5 therefore has a
knowledge layer before any richer ranker is allowed to become active.

- rated 8-10 and 1-4 titles receive the highest metadata priority;
- the queue is persistent and uses the existing Metadata Doctor providers;
- candidate-frontier titles are queued separately;
- provider I/O remains bounded and resumable;
- V5 reports factual coverage and keeps the rich ranker inactive until the informative examples
  have enough premise/country coverage.

This is model input quality, not poster polish.

## Open-world discovery

The local IMDb-derived catalog is a fast index, not the universe of possible recommendations.
V5 can refresh a bounded TMDb neighbourhood from highly rated anchors, import only candidates with
a stable IMDb identity, persist minimal metadata, and then score them through the normal local
pipeline. Recommendation requests never wait on the network: they consume only the cached lane.

Online discovery is timestamped. Historical replay excludes candidates discovered after the replay
date, so current TMDb relationships cannot be injected into past evaluations.

Metadata acquisition is **coverage-aware**: positive and negative extremes start from comparable
priority, then the less-documented class receives a deficit boost. On a profile where dislikes have
far less premise/country coverage than favourites, Metadata Doctor therefore learns the rejection
boundary first instead of spending most of its provider budget on already-richer positive examples.

## Two complementary replay gates

V5 keeps the non-overlapping rolling benchmark for broad regression/leakage protection, but it also
adds date-aligned event replay. The latter hides the whole historical rating day and all later
ratings, evaluates the engine on the actual date, then scores only that day's outcomes.

This matters because CineCalendar is explicitly calendar-aware. A January recommendation should not
be penalized for failing to rank a Christmas film the user watches eleven months later.

Neither replay mode can promote code by itself; stable promotion requires agreement across the
broad rolling guardrail and date-aligned decision replay.
# Recomandări românești în Alpha 16

Eligibilitatea rămâne separată de scorul de recomandare. Pentru fiecare IMDb ID,
cache-ul păstrează sursele care indică limba română și originea România,
numărul de furnizori independenți și eventualele contradicții. Detaliile TMDb
deja descărcate, legate prin ID TMDb și, când există, ID IMDb, pot confirma sau
contrazice explicit limba originală și țările de producție. Titlurile cu astfel
de contradicții sunt reținute pentru verificare și nu intră în recomandări;
absența unui titlu din rezultatul unei surse nu este considerată contradicție.
Un rezultat doar din căutarea IMDb nu dovedește limba originală și așteaptă
confirmarea unei surse care oferă explicit această informație.
Pe card, explicația de eligibilitate enumeră sursele confirmării. Datele locale
doar despre țară rămân suport, fără a permite singure admiterea unui film.

## Navigare și stabilitate în Alpha 17

Navigarea între pagini reutilizează rezultatele calculate cât timp data și starea
utilizatorului nu s-au schimbat. Recalcularea explicită creează un tur nou; listele
Watchlist, de cinema românesc și programul zilei se actualizează când se schimbă
datele relevante. Rezultatele unui worker pornit pe o stare veche nu mai înlocuiesc
selecția curentă. Închiderea oprește și workerii paginilor, inclusiv cei porniți
pentru catalogul românesc. Bara laterală poate fi derulată la ferestre mai mici.
Căutarea IMDb pentru titluri românești împarte intervalele care ating limita de
rezultate, ca să nu piardă în tăcere filme din catalog.
