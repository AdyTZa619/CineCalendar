from __future__ import annotations
from collections import defaultdict
from .models import Movie
from .util import normalize_text

THEME_PATTERNS: dict[str, tuple[str, ...]] = {
    "christianity": ("christian", "crestin", "jesus", "hristos", "christ", "bible", "biblic", "gospel", "evanghel", "church", "biseric"),
    "faith": ("faith", "credinta", "spiritual", "religious", "religios", "divine", "dumneze"),
    "cross_veneration": ("holy cross", "true cross", "sfanta cruce", "inaltarea crucii", "exaltation of the cross"),
    "passion_of_christ": ("passion of christ", "patimile", "crucifix", "golgotha", "calvary"),
    "monasticism": ("monk", "monastery", "monastic", "calugar", "manastir", "nun ", "abbey", "ascetic"),
    "saints": ("saint", "sfant", "martyr", "martir"),
    "war": ("war", "razboi", "battle", "frontline", "soldier", "army", "military", "occupation", "invasion"),
    "peace": ("peace", "pace", "pacif", "ceasefire", "armistice"),
    "reconciliation": ("reconcil", "iertare", "forgiven", "forgiveness", "truces", "truce"),
    "holocaust": ("holocaust", "shoah", "auschwitz", "nazi camp", "concentration camp", "deportation"),
    "communism": ("communis", "ceau", "soviet", "ussr", "urss", "iron curtain", "securitate"),
    "revolution": ("revolution", "revolutie", "uprising", "revolt", "insurrection"),
    "existential": ("existential", "meaning of life", "sensul vietii", "absurd", "mortality", "metaphys"),
    "death": ("death", "moarte", "funeral", "mortality", "dying"),
    "epidemic": ("plague", "epidemic", "pandemic", "ciuma", "pestilence"),
    "family": ("family", "familie", "father", "mother", "parent", "sibling", "children"),
    "survival": ("surviv", "supraviet", "stranded", "wilderness", "disaster"),
    "crime": ("crime", "murder", "killer", "detective", "police", "mafia", "gangster", "investigation"),
    "folk_horror": ("folk horror", "pagan ritual", "folklore horror", "witch", "cult ritual"),
    "nature": ("nature", "wildlife", "forest", "mountain", "ocean", "river", "wilderness", "natura", "padure", "munte"),
    "romania": ("romania", "romanian", "romanesc", "bucharest", "bucuresti", "transylvania", "transilvania", "carpath"),
    "balkans": ("balkan", "balcani", "serbia", "bulgaria", "yugoslav", "bosnia", "croatia", "albania", "macedonia"),
    "antiquity": ("ancient", "antiquity", "roman empire", "greek empire", "dacia", "dacian", "antich"),
    "medieval": ("medieval", "middle ages", "evul mediu", "knight", "crusade", "plague"),
    "biography": ("biograph", "true story", "life of", "portret", "memoir"),
    "history": ("histor", "period drama", "epoca", "war", "revolution"),
    "politics": ("politic", "election", "government", "dictator", "president", "parliament"),
    "autumn": ("autumn", "fall season", "toamna", "harvest", "frunze"),
    "winter": ("winter", "snow", "iarna", "craciun", "christmas", "new year"),
    "spring": ("spring", "primavara", "easter", "pasti"),
    "summer": ("summer", "vara", "vacation", "beach", "seaside"),
    "christmas": ("christmas", "craciun", "nativity", "nasterea domnului", "santa"),
    "easter": ("easter", "pasti", "resurrection", "invierea"),
    "halloween": ("halloween", "all hallows", "pumpkin", "trick or treat"),
    "contemplative": ("contemplative", "meditative", "slow cinema", "poetic", "spiritual journey"),
    "dark": ("dark", "bleak", "grim", "disturbing", "tragic", "sombre", "somber"),
    "hopeful": ("hope", "uplifting", "redemption", "inspiring", "solidarity"),
    "melancholic": ("melanchol", "nostalgi", "regret", "loneliness", "solitude"),
    "violence": ("violent", "violence", "brutal", "gore", "massacre", "murder"),
    "humor": ("comedy", "comic", "satire", "satirical", "humor", "funny"),
    "romance": ("romance", "romantic", "love story", "fall in love", "relationship", "iubire", "dragoste"),
}

GENRE_THEME = {
    "History": {"history": 1.0}, "War": {"war": 1.0, "history": .4}, "Documentary": {"documentary": 1.0},
    "Biography": {"biography": 1.0}, "Crime": {"crime": 1.0}, "Horror": {"horror": 1.0, "dark": .35},
    "Comedy": {"humor": 1.0}, "Romance": {"romance": 1.0}, "Sci-Fi": {"scifi": 1.0},
    "Fantasy": {"fantasy": 1.0}, "Adventure": {"adventure": 1.0}, "Thriller": {"thriller": 1.0},
    "Drama": {"drama": .8}, "Family": {"family": .8}, "Mystery": {"mystery": .9},
}


def extract_semantic(movie: Movie) -> dict[str, float]:
    text = " ".join([movie.title, movie.original_title, movie.overview, " ".join(movie.keywords)]).lower()
    normalized = normalize_text(text)
    scores: dict[str, float] = defaultdict(float)
    for genre in movie.genres:
        for tag, w in GENRE_THEME.get(genre, {}).items():
            scores[tag] = max(scores[tag], w)
    for tag, patterns in THEME_PATTERNS.items():
        hits = sum(1 for p in patterns if normalize_text(p) in normalized or p.lower() in text)
        if hits:
            scores[tag] = max(scores[tag], min(1.0, 0.48 + 0.18 * hits))
    if movie.title_type and "documentary" in movie.title_type.lower():
        scores["documentary"] = 1.0
    return dict(scores)


def runtime_bucket(minutes: int | None) -> str:
    if minutes is None: return "unknown"
    if minutes < 90: return "<90"
    if minutes <= 120: return "90-120"
    if minutes <= 150: return "121-150"
    return ">150"


def popularity_bucket(votes: int | None) -> str:
    if votes is None: return "unknown"
    if votes < 1_000: return "obscure"
    if votes < 10_000: return "niche"
    if votes < 100_000: return "known"
    return "popular"


def feature_vector(movie: Movie) -> dict[str, float]:
    """Sparse representation used by both profile learning and candidate scoring.

    Besides single attributes, a small set of interaction features captures taste such
    as History+War or Crime+Thriller. With thousands of explicit ratings these are much
    more informative than treating every genre independently.
    """
    sem = movie.semantic or extract_semantic(movie)
    vec: dict[str, float] = {}
    genres = sorted({normalize_text(g) for g in movie.genres if normalize_text(g)})
    for g in genres:
        vec[f"genre:{g}"] = 1.0
    # Pairwise genre interactions: bounded because IMDb movies normally have few genres.
    for i, a in enumerate(genres):
        for b in genres[i+1:]:
            vec[f"combo:genre:{a}|genre:{b}"] = .90
    for tag, weight in sem.items():
        vec[f"theme:{tag}"] = float(weight)
    directors = [normalize_text(d) for d in movie.directors if normalize_text(d)]
    for d in directors:
        vec[f"director:{d}"] = .75
        # Director+genre captures cases where the user likes a director especially in a certain lane.
        for g in genres[:4]:
            vec[f"combo:director:{d}|genre:{g}"] = .72
    for c in movie.countries:
        nc = normalize_text(c)
        if nc: vec[f"country:{nc}"] = .7
    if movie.year:
        decade = f"{movie.year//10*10}s"
        vec[f"decade:{decade}"] = .6
        for g in genres[:4]:
            vec[f"combo:genre:{g}|decade:{decade}"] = .52
    vec[f"runtime:{runtime_bucket(movie.runtime_min)}"] = .55
    vec[f"popularity:{popularity_bucket(movie.num_votes)}"] = .4
    return vec
