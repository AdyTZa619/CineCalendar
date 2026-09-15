from __future__ import annotations

import re
from datetime import date

from .recommender_v6 import FastRecommendationEngineV6
from .semantic import extract_semantic
from .util import clamp, normalize_text


ENGINE_VERSION = "7.0.0"

# Calendar relevance is precision-first. Every concrete event must have its own subject
# anchor. Broad labels such as History, War, Drama, Christianity, Romania or Politics are
# never enough by themselves. If an event is not mapped here, factual calendar relevance is
# intentionally zero until an explicit rule is added.
#
# Rule fields:
#   kind: directă / istorică / spirituală
#   label: human explanation shown in the UI
#   terms: event-specific phrases searched in title/original title/overview/keywords
#   any_tags: distinctive semantic tags, when one is sufficient
#   all_tag_groups: alternative groups for which all listed semantic tags must be present
EVENT_RELEVANCE_RULES: dict[str, dict] = {
    # Orthodox feasts and periods from the compact calendar.
    "nativity": {
        "kind": "directă", "label": "Nașterea lui Hristos",
        "terms": ("nativity of jesus", "birth of jesus", "birth of christ", "nasterea domnului", "nasterea lui iisus", "nasterea lui isus"),
        "all_tag_groups": (("christmas", "christianity"),),
    },
    "epiphany": {
        "kind": "directă", "label": "Botezul Domnului",
        "terms": ("baptism of jesus", "baptism of christ", "botezul domnului", "theophany of christ", "epiphany of jesus"),
    },
    "meeting": {
        "kind": "directă", "label": "Întâmpinarea Domnului",
        "terms": ("presentation of jesus", "presentation of christ", "presentation in the temple", "intampinarea domnului", "simeon and anna"),
    },
    "annunciation": {
        "kind": "directă", "label": "Buna Vestire",
        "terms": ("annunciation", "buna vestire", "annunciation to mary", "gabriel and mary"),
    },
    "transfiguration": {
        "kind": "directă", "label": "Schimbarea la Față a lui Hristos",
        "terms": ("transfiguration of jesus", "transfiguration of christ", "schimbarea la fata", "mount tabor"),
    },
    "dormition": {
        "kind": "directă", "label": "Adormirea Maicii Domnului",
        "terms": ("dormition of mary", "dormition of the theotokos", "assumption of mary", "adormirea maicii domnului", "death of virgin mary"),
    },
    "nativity_theotokos": {
        "kind": "directă", "label": "Nașterea Maicii Domnului",
        "terms": ("nativity of mary", "birth of virgin mary", "birth of mary", "nasterea maicii domnului"),
    },
    "exaltation_cross": {
        "kind": "directă", "label": "Sfânta Cruce / Patimile și Răstignirea lui Hristos",
        "terms": ("holy cross", "true cross", "exaltation of the cross", "sfanta cruce", "inaltarea sfintei cruci", "calvary", "golgotha", "crucifixion of jesus"),
        "any_tags": ("cross_veneration", "passion_of_christ"),
    },
    "st_andrew": {
        "kind": "directă", "label": "Sf. Apostol Andrei",
        "terms": ("saint andrew", "st andrew apostle", "apostle andrew", "sfantul andrei", "sf apostol andrei"),
    },
    "st_nicholas": {
        "kind": "directă", "label": "Sf. Nicolae",
        "terms": ("saint nicholas", "st nicholas", "sfantul nicolae", "sf nicolae"),
    },
    "palm_sunday": {
        "kind": "directă", "label": "Floriile / Intrarea Domnului în Ierusalim",
        "terms": ("palm sunday", "triumphal entry", "entry into jerusalem", "floriile", "intrarea in ierusalim"),
    },
    "holy_week": {
        "kind": "directă", "label": "Săptămâna Patimilor",
        "terms": ("holy week", "passion week", "saptamana patimilor", "calvary", "golgotha", "crucifixion of jesus"),
        "any_tags": ("passion_of_christ",),
    },
    "easter": {
        "kind": "directă", "label": "Învierea lui Hristos / Sfintele Paști",
        "terms": ("resurrection of jesus", "resurrection of christ", "invierea lui hristos", "sfintele pasti"),
        "all_tag_groups": (("easter", "christianity"),),
    },
    "ascension": {
        "kind": "directă", "label": "Înălțarea Domnului",
        "terms": ("ascension of jesus", "ascension of christ", "inaltarea domnului"),
    },
    "pentecost": {
        "kind": "directă", "label": "Rusaliile / Pogorârea Duhului Sfânt",
        "terms": ("pentecost", "descent of the holy spirit", "holy spirit and apostles", "rusalii", "pogorarea duhului sfant"),
    },
    "great_lent": {
        "kind": "spirituală", "label": "Postul Mare / nevoință creștină",
        "terms": ("great lent", "lent fasting", "orthodox lent", "postul mare", "christian fasting"),
        "any_tags": ("monasticism",),
    },

    # Secular / historical anchors from the compact calendar.
    "holocaust_day": {
        "kind": "istorică", "label": "Holocaust",
        "terms": ("holocaust", "shoah", "auschwitz", "concentration camp", "nazi camp"),
        "any_tags": ("holocaust",),
    },
    "earth_day": {
        "kind": "directă", "label": "mediu / natură / ecologie",
        "terms": ("earth day", "environmental", "environment", "ecology", "climate change", "conservation", "pollution"),
        "any_tags": ("nature",),
    },
    "europe_day": {
        "kind": "istorică", "label": "Europa unită / integrarea europeană",
        "terms": ("europe day", "european union", "european integration", "schuman declaration", "formation of the eu"),
    },
    "children_day": {
        "kind": "directă", "label": "copii / copilărie",
        "terms": ("children", "childhood", "child welfare", "children rights", "kids growing up", "copilarie", "copii"),
    },
    "peace_day": {
        "kind": "directă", "label": "pace / reconciliere",
        "terms": ("international day of peace", "peace movement", "peace process", "reconciliation", "ceasefire"),
        "any_tags": ("peace", "reconciliation"),
    },
    "tourism_day": {
        "kind": "directă", "label": "călătorie / turism",
        "terms": ("tourism", "travel documentary", "travelling", "traveling", "backpacking", "road trip", "world traveler", "world traveller"),
    },
    "halloween": {
        "kind": "directă", "label": "Halloween / horror sezonier",
        "terms": ("halloween", "all hallows", "trick or treat", "pumpkin"),
        "any_tags": ("halloween", "folk_horror"),
    },
    "armistice": {
        "kind": "istorică", "label": "Armistițiul din 1918 / Primul Război Mondial",
        "terms": ("armistice 1918", "1918 armistice", "first world war", "world war i", "wwi", "great war"),
    },
    "romania_national": {
        "kind": "istorică", "label": "Marea Unire din 1918 / 1 Decembrie",
        "terms": ("marea unire", "great union 1918", "1 december 1918", "december 1 1918", "alba iulia 1918", "transylvania union romania", "romanian unification 1918"),
    },
    "romanian_revolution": {
        "kind": "istorică", "label": "Revoluția Română din 1989",
        "terms": ("romanian revolution", "revolutia romana", "romania 1989", "ceausescu 1989", "bucharest december 1989", "timisoara 1989"),
        "all_tag_groups": (("revolution", "communism"),),
    },

    # Additional Orthodox observances from RichCalendarEngine.
    "new_year_basil": {
        "kind": "directă", "label": "Tăierea împrejur a Domnului / Sf. Vasile cel Mare",
        "terms": ("circumcision of jesus", "circumcision of christ", "taierea imprejur a domnului", "saint basil the great", "basil the great"),
    },
    "st_john_baptist": {
        "kind": "directă", "label": "Sf. Ioan Botezătorul",
        "terms": ("john the baptist", "saint john the baptist", "st john the baptist", "ioan botezatorul"),
    },
    "three_hierarchs": {
        "kind": "directă", "label": "Sfinții Trei Ierarhi",
        "terms": ("three holy hierarchs", "three hierarchs", "john chrysostom", "gregory nazianzus", "gregory the theologian", "basil the great"),
    },
    "st_george": {
        "kind": "directă", "label": "Sf. Mare Mucenic Gheorghe",
        "terms": ("saint george martyr", "st george martyr", "saint george the martyr", "sfantul gheorghe", "sf gheorghe"),
    },
    "constantine_helena": {
        "kind": "directă", "label": "Sfinții Împărați Constantin și Elena",
        "terms": ("constantine and helena", "constantine and helen", "constantine the great", "emperor constantine", "saint helena", "st helena"),
    },
    "nativity_john": {
        "kind": "directă", "label": "Nașterea Sf. Ioan Botezătorul",
        "terms": ("birth of john the baptist", "nativity of john the baptist", "john the baptist", "nasterea sf ioan botezatorul"),
    },
    "peter_paul": {
        "kind": "directă", "label": "Sfinții Apostoli Petru și Pavel",
        "terms": ("saint peter apostle", "st peter apostle", "apostle peter", "saint paul apostle", "st paul apostle", "apostle paul", "peter and paul apostles"),
    },
    "st_elijah": {
        "kind": "directă", "label": "Sf. Proroc Ilie",
        "terms": ("prophet elijah", "prophet elias", "biblical elijah", "prorocul ilie", "sfantul ilie"),
    },
    "dormition_fast": {
        "kind": "spirituală", "label": "Postul Adormirii Maicii Domnului",
        "terms": ("dormition fast", "fast of the theotokos", "postul adormirii", "fasting for the dormition"),
        "any_tags": ("monasticism",),
    },
    "beheading_john": {
        "kind": "directă", "label": "Tăierea capului Sf. Ioan Botezătorul",
        "terms": ("beheading of john the baptist", "death of john the baptist", "taierea capului sf ioan botezatorul", "john the baptist beheaded"),
    },
    "protection_theotokos": {
        "kind": "directă", "label": "Acoperământul Maicii Domnului",
        "terms": ("protection of the theotokos", "intercession of the theotokos", "pokrov", "acoperamantul maicii domnului"),
    },
    "st_demetrius": {
        "kind": "directă", "label": "Sf. Mare Mucenic Dimitrie",
        "terms": ("saint demetrius", "st demetrius", "demetrius of thessaloniki", "sfantul dimitrie", "sf dimitrie"),
    },
    "st_dimitrie_bas": {
        "kind": "directă", "label": "Sf. Dimitrie cel Nou",
        "terms": ("dimitrie cel nou", "demetrius the new", "saint demetrius the new", "st dimitrie basarabov"),
    },
    "archangels": {
        "kind": "directă", "label": "Sfinții Arhangheli Mihail și Gavriil",
        "terms": ("archangel michael", "archangel gabriel", "michael and gabriel archangels", "arhanghelul mihail", "arhanghelul gavriil"),
    },
    "nativity_fast": {
        "kind": "spirituală", "label": "Postul Nașterii Domnului",
        "terms": ("nativity fast", "christmas fast", "orthodox advent", "postul nasterii domnului", "christian fasting"),
        "any_tags": ("monasticism",),
    },
    "synaxis_theotokos": {
        "kind": "directă", "label": "Maica Domnului / Născătoarea de Dumnezeu",
        "terms": ("synaxis of the theotokos", "virgin mary", "mother of god", "theotokos", "maica domnului"),
    },
    "st_stephen": {
        "kind": "directă", "label": "Sf. Arhidiacon Ștefan",
        "terms": ("saint stephen", "st stephen martyr", "stephen protomartyr", "sfantul stefan", "sf stefan"),
    },
    "lazarus_saturday": {
        "kind": "directă", "label": "Învierea lui Lazăr / Sâmbăta lui Lazăr",
        "terms": ("raising of lazarus", "resurrection of lazarus", "lazarus saturday", "sambata lui lazar"),
    },
    "holy_thursday": {
        "kind": "directă", "label": "Joia Mare / Cina cea de Taină / Patimile",
        "terms": ("holy thursday", "maundy thursday", "last supper", "joia mare", "cina cea de taina", "calvary", "golgotha"),
        "any_tags": ("passion_of_christ",),
    },
    "good_friday": {
        "kind": "directă", "label": "Vinerea Mare / Răstignirea lui Hristos",
        "terms": ("good friday", "vinerea mare", "crucifixion of jesus", "calvary", "golgotha"),
        "any_tags": ("passion_of_christ", "cross_veneration"),
    },
    "holy_saturday": {
        "kind": "directă", "label": "Sâmbăta Mare / Patimile lui Hristos",
        "terms": ("holy saturday", "sambata mare", "burial of jesus", "tomb of jesus", "calvary", "golgotha"),
        "any_tags": ("passion_of_christ",),
    },
    "bright_week": {
        "kind": "spirituală", "label": "Săptămâna Luminată / Învierea lui Hristos",
        "terms": ("bright week", "resurrection of jesus", "resurrection of christ", "saptamana luminata"),
        "all_tag_groups": (("easter", "christianity"),),
    },
    "all_saints": {
        "kind": "directă", "label": "sfinți / martiri creștini",
        "terms": ("all saints", "all saints sunday", "toti sfintii"),
        "any_tags": ("saints",),
    },
    "apostles_fast": {
        "kind": "spirituală", "label": "Postul Sfinților Apostoli Petru și Pavel",
        "terms": ("apostles fast", "fast of peter and paul", "postul sfintilor apostoli", "peter and paul apostles"),
        "any_tags": ("monasticism",),
    },

    # Romanian civic, cultural and historical anchors.
    "romanian_culture": {
        "kind": "istorică", "label": "Mihai Eminescu / cultura română",
        "terms": ("mihai eminescu", "eminescu", "ziua culturii nationale", "romanian national culture day"),
    },
    "union_principalities": {
        "kind": "istorică", "label": "Unirea Principatelor din 1859",
        "terms": ("alexandru ioan cuza", "union of the principalities", "unirea principatelor", "moldavia wallachia union", "1859 romania", "little union"),
    },
    "brancusi_day": {
        "kind": "istorică", "label": "Constantin Brâncuși",
        "terms": ("constantin brancusi", "brancusi"),
    },
    "martisor": {
        "kind": "directă", "label": "Mărțișor / tradiție românească",
        "terms": ("martisor", "mărțișor", "romanian spring tradition", "red and white string romania"),
    },
    "womens_day": {
        "kind": "directă", "label": "drepturile și emanciparea femeilor",
        "terms": ("international womens day", "international women's day", "women rights", "women's rights", "womens rights", "feminism", "female emancipation", "women movement"),
    },
    "labour_day": {
        "kind": "istorică", "label": "mișcarea muncitorească / drepturile lucrătorilor",
        "terms": ("labor movement", "labour movement", "workers movement", "workers rights", "worker rights", "trade union", "labor strike", "labour strike", "may day workers"),
    },
    "victory_europe": {
        "kind": "istorică", "label": "sfârșitul celui de-Al Doilea Război Mondial în Europa",
        "terms": ("victory in europe", "ve day", "v e day", "end of world war ii in europe", "end of wwii in europe", "german surrender 1945", "nazi surrender 1945"),
    },
    "romanian_independence": {
        "kind": "istorică", "label": "Independența României / Războiul din 1877",
        "terms": ("romanian independence", "romanian war of independence", "war of independence romania", "romania 1877", "carol i 1877", "independenta romaniei"),
    },
    "environment_day": {
        "kind": "directă", "label": "mediu / ecologie",
        "terms": ("world environment day", "environmental", "environment", "ecology", "climate change", "conservation", "pollution"),
    },
    "sanziene": {
        "kind": "directă", "label": "Sânziene / Drăgaica / tradiție românească",
        "terms": ("sanziene", "sânziene", "dragaica", "drăgaica", "romanian midsummer tradition"),
    },
    "flag_day_ro": {
        "kind": "istorică", "label": "Drapelul României",
        "terms": ("romanian flag", "flag of romania", "drapelul romaniei", "romanian tricolor", "tricolorul romanesc"),
    },
    "anthem_day_ro": {
        "kind": "istorică", "label": "Imnul Național al României",
        "terms": ("desteapta te romane", "romanian national anthem", "national anthem of romania", "imnul national al romaniei"),
    },
    "august_23_1944": {
        "kind": "istorică", "label": "23 August 1944 / actul Regelui Mihai",
        "terms": ("23 august 1944", "king michael coup", "michael coup 1944", "romania 1944 coup", "romania switches sides", "romania changed sides 1944"),
    },
    "ww2_start": {
        "kind": "istorică", "label": "începutul celui de-Al Doilea Război Mondial",
        "terms": ("invasion of poland 1939", "german invasion of poland", "september 1939 poland", "start of world war ii", "outbreak of world war ii", "outbreak of wwii"),
    },
    "september_11": {
        "kind": "istorică", "label": "atentatele din 11 septembrie 2001",
        "terms": ("september 11 attacks", "september 11 2001", "9 11 attacks", "world trade center attacks", "twin towers 2001", "9/11 attacks"),
    },
    "army_day_ro": {
        "kind": "istorică", "label": "Armata României",
        "terms": ("romanian army", "romanian military", "armata romana", "romanian soldiers", "romanian armed forces"),
    },
    "berlin_wall": {
        "kind": "istorică", "label": "Zidul Berlinului / căderea lui în 1989",
        "terms": ("berlin wall", "fall of the berlin wall", "1989 berlin wall", "zidul berlinului"),
    },
    "human_rights": {
        "kind": "directă", "label": "drepturile omului",
        "terms": ("human rights", "universal declaration of human rights", "civil rights", "human rights activist", "drepturile omului"),
    },
}

# Events whose meaning is intentionally atmospheric/seasonal rather than factual. They may
# populate the separate seasonal/atmosphere lanes, but never masquerade as factual matches.
SEASONAL_EVENT_KEYS = {
    "spring_equinox", "summer_solstice", "autumn_equinox", "winter_solstice", "new_year_eve",
}


class FastRecommendationEngineV7(FastRecommendationEngineV6):
    """Calendar recommender with a global event-specific precision gate.

    Every non-seasonal event is required to have an explicit rule above. A movie must match
    that concrete subject before it can receive direct, historical or spiritual relevance.
    This prevents the entire class of false positives where a generic History/War/Drama/
    Christianity/Romania label was treated as a connection with a specific date.
    """

    @staticmethod
    def _movie_text(movie) -> str:
        bits = [movie.title or "", movie.original_title or "", movie.overview or ""]
        bits.extend(str(x) for x in (movie.keywords or []))
        return normalize_text(" ".join(bits))

    @staticmethod
    def _contains_term(text: str, term: str) -> bool:
        nt = normalize_text(term)
        if not nt:
            return False
        return re.search(r"(?<!\w)" + re.escape(nt) + r"(?!\w)", text) is not None

    def _anchor_strength(self, rule: dict, sem: dict[str, float], text: str) -> float:
        strength = 0.0
        for term in rule.get("terms", ()):
            if self._contains_term(text, term):
                strength = 1.0
                break

        for tag in rule.get("any_tags", ()):
            strength = max(strength, float(sem.get(tag, 0.0) or 0.0))

        for group in rule.get("all_tag_groups", ()):
            vals = [float(sem.get(tag, 0.0) or 0.0) for tag in group]
            if vals and all(v > 0 for v in vals):
                strength = max(strength, min(vals))
        return clamp(strength)

    def _seasonal_relation(self, ev, proximity: float, sem: dict[str, float]):
        atmosphere = max((float(sem.get(t, 0.0) or 0.0) for t in ev.atmosphere_tags), default=0.0)
        if atmosphere <= 0:
            return 0.0, "slabă", "Fără legătură sezonieră verificabilă."
        score = clamp(atmosphere * .46 * float(ev.importance) * float(proximity))
        if score < .12:
            return 0.0, "slabă", "Fără legătură sezonieră verificabilă."
        return score, "atmosferică", f"{ev.name}: potrivire sezonieră/atmosferică."

    def _event_relation(self, ev, proximity: float, movie, sem: dict[str, float]):
        if ev.key in SEASONAL_EVENT_KEYS or ev.category == "sezon":
            return self._seasonal_relation(ev, proximity, sem)

        rule = EVENT_RELEVANCE_RULES.get(ev.key)
        if rule is None:
            # Fail closed: no explicit event rule means no factual recommendation.
            return 0.0, "slabă", "Eveniment fără regulă de relevanță factuală verificată."

        anchor = self._anchor_strength(rule, sem, self._movie_text(movie))
        if anchor < .48:
            return 0.0, "slabă", "Fără legătură specifică verificabilă cu evenimentul."

        kind = str(rule.get("kind") or "directă")
        multiplier = {"directă": 1.0, "istorică": .82, "spirituală": .72}.get(kind, .70)
        score = clamp(anchor * multiplier * float(ev.importance) * float(proximity))
        if score < .12:
            return 0.0, "slabă", "Fără legătură specifică verificabilă cu evenimentul."

        label = str(rule.get("label") or ev.name)
        return score, kind, f"{ev.name}: legătură {kind} prin {label}."

    def _calendar_score_cached(self, movie, when: date):
        sem = movie.semantic or extract_semantic(movie)
        events, _season_label, _season_tags = self._date_context(when)
        best = (0.0, "slabă", "Fără reper calendaristic specific verificat.")
        for ev, proximity in events:
            relation = self._event_relation(ev, proximity, movie, sem)
            if relation[0] > best[0]:
                best = relation
        return best
