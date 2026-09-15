from __future__ import annotations
import hashlib, json, math, os, re, sys, unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = str(value).strip()
    if not value or value in {"\\N", "N/A", "None"}:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def to_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip() in {"", "\\N"}:
            return default
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() in {"", "\\N"}:
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def split_csvish(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in re.split(r"[,|;]", str(value)) if x.strip()]


def normalize_text(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def identity_key(title: str, original_title: str | None, year: int | None, title_type: str | None) -> str:
    parts = [normalize_text(title), normalize_text(original_title or title), str(year or ""), normalize_text(title_type or "movie")]
    return "|".join(parts)


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def cosine_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data: Path
    logs: Path
    cache: Path
    backups: Path

    @classmethod
    def portable(cls) -> "AppPaths":
        override = os.environ.get("CINECALENDAR_DATA_DIR")
        if override:
            root = Path(override).expanduser().resolve()
        else:
            base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd().resolve()
            root = base / "CineCalendarData"
        data = root / "data"
        logs = root / "logs"
        cache = root / "cache"
        backups = root / "backups"
        for p in (root, data, logs, cache, backups):
            p.mkdir(parents=True, exist_ok=True)
        return cls(root, data, logs, cache, backups)
