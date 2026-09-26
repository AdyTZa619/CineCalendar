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
