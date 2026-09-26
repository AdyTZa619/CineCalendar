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

## Two complementary replay gates

V5 keeps the non-overlapping rolling benchmark for broad regression/leakage protection, but it also
adds date-aligned event replay. The latter hides the whole historical rating day and all later
ratings, evaluates the engine on the actual date, then scores only that day's outcomes.

This matters because CineCalendar is explicitly calendar-aware. A January recommendation should not
be penalized for failing to rank a Christmas film the user watches eleven months later.

Neither replay mode can promote code by itself; stable promotion requires agreement across the
broad rolling guardrail and date-aligned decision replay.
