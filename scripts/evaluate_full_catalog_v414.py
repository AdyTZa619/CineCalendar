from __future__ import annotations

import argparse
import json
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.full_catalog_evaluation_v414 import run_full_catalog_replay
from cinecalendar.quality_manager_v47 import RecommendationQualityManagerV47
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.util import AppPaths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluează full-catalog challengerul pe ferestre istorice fără să schimbe producția."
    )
    parser.add_argument("--db", default="", help="Calea către cinecalendar.db; implicit folosește datele portabile.")
    parser.add_argument("--folds", type=int, default=3, help="Număr dorit de ferestre temporale.")
    parser.add_argument("--candidate-limit", type=int, default=600, help="Buget fix de candidați per parte.")
    parser.add_argument("--injection-share", type=float, default=.15, help="Pondere challenger în blendul de test.")
    parser.add_argument("--als-timeout", type=float, default=180.0)
    parser.add_argument("--output", default="", help="Fișier JSON opțional pentru raport.")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser() if args.db else (AppPaths.portable().data / "cinecalendar.db")
    if not db_path.is_file():
        raise SystemExit(f"Baza nu există: {db_path}")

    db = Database(db_path)
    manager = RecommendationQualityManagerV47(db)
    engine_cls = manager.preferred_engine_class()
    if not isinstance(engine_cls, type) or not issubclass(engine_cls, FastRecommendationEngineV16):
        engine_cls = FastRecommendationEngineV16

    report = run_full_catalog_replay(
        db_path,
        engine_cls=engine_cls,
        candidate_limit=max(100, int(args.candidate_limit)),
        desired_folds=max(2, int(args.folds)),
        als_timeout=max(1.0, float(args.als_timeout)),
        injection_share=max(.01, min(.40, float(args.injection_share))),
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        print(output)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
