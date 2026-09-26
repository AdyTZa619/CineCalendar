from __future__ import annotations

import argparse
import json
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.metadata_doctor import process_metadata_queue
from cinecalendar.v5_knowledge import V5KnowledgeBase


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the V5 taste-knowledge base with bounded Metadata Doctor passes."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--seed-limit", type=int, default=0)
    parser.add_argument(
        "--include-neutral",
        action="store_true",
        help="Include 5-7 ratings after the positive/negative boundary is sufficiently covered.",
    )
    args = parser.parse_args()

    db = Database(Path(args.db).expanduser())
    knowledge = V5KnowledgeBase(db)
    seeded = knowledge.seed_profile(
        limit=max(0, int(args.seed_limit)),
        informative_only=not bool(args.include_neutral),
    )
    token = str(db.get_setting("tmdb_token", "") or "").strip()

    processed = []
    for _ in range(max(0, int(args.passes))):
        result = process_metadata_queue(
            db,
            token,
            limit=max(1, min(50, int(args.batch))),
            force=False,
        )
        processed.append(
            {
                "attempted": result.attempted,
                "completed": result.completed,
                "improved": result.improved,
                "retrying": result.retrying,
                "failed": result.failed,
            }
        )
        if result.attempted <= 0:
            break

    print(
        json.dumps(
            {
                "seeded": seeded,
                "processed": processed,
                "knowledge": knowledge.status(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
