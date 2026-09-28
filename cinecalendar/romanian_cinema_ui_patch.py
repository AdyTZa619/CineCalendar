from __future__ import annotations

from datetime import date

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

from .qt_ui import WorkerThread


def romanian_diagnostic_lines(status: dict, displayed: int) -> list[str]:
    """Report measured stages, without treating different units as interchangeable."""
    counts = dict(status.get("diagnostics") or {})
    lines = (
        ["Sursele nu au fost încă verificate."]
        if status.get("source") == "not-verified" else
        [f"Identificatori IMDb confirmați: {int(status.get('external_count', 0) or 0):,}."]
    )
    if "local_catalog_matches" in counts:
        lines.append(f"Titluri potrivite găsite în catalogul local: {int(counts['local_catalog_matches']):,}.")
    if "unseen_catalog_rows" in counts:
        lines.append(f"Rămase după excluderea celor văzute/evaluate sau respinse: {int(counts['unseen_catalog_rows']):,}.")
    if "scored_candidates" in counts:
        lines.append(f"Cu scor personal eligibil: {int(counts['scored_candidates']):,}.")
        reasons = (
            ("metadate IMDb insuficiente", "quality_filtered"),
            ("filtru de conținut", "policy_filtered"),
            ("predicție personală prea slabă", "low_prediction_filtered"),
            ("verificarea ALS", "als_guard_filtered"),
        )
        removed = [f"{label}: {int(counts[key]):,}" for label, key in reasons if int(counts.get(key, 0) or 0)]
        if removed:
            lines.append("Excluse înainte de clasare — " + " • ".join(removed) + ".")
    lines.append(f"Afișate acum: {int(displayed):,}.")
    if status.get("error"):
        lines.append("Unele surse nu au răspuns; baza confirmată poate fi incompletă.")
    return lines


def install_romanian_cinema_ui_patch(window_cls) -> None:
    """Add a dedicated, precision-first Romanian cinema page."""
    if getattr(window_cls, "_romanian_cinema_patch_installed", False):
        return

    nav = list(window_cls.NAV)
    if not any(key == "romanian" for key, _label in nav):
        position = next((i + 1 for i, (key, _label) in enumerate(nav) if key == "recommendations"), 2)
        nav.insert(position, ("romanian", "Recomandări românești"))
        window_cls.NAV = nav

    def _recalculate_romanian(self):
        worker = getattr(self, "romanian_worker", None)
        refresh = getattr(self, "romanian_refresh_worker", None)
        if (worker is not None and worker.isRunning()) or (refresh is not None and refresh.isRunning()):
            self.set_status("Selecția românească este deja în curs de calcul.", True)
            return
        self.romanian_result = None
        self.romanian_cache_signature = None
        invalidate = getattr(self.s.recommender, "invalidate_round", None)
        if callable(invalidate):
            invalidate("romanian")
        self.show_page("romanian")

    def _refresh_romanian_sources(self):
        ranking = getattr(self, "romanian_worker", None)
        refresh = getattr(self, "romanian_refresh_worker", None)
        if (ranking is not None and ranking.isRunning()) or (refresh is not None and refresh.isRunning()):
            self.set_status("Așteaptă terminarea verificării românești în curs.", True)
            return

        recommender = self.s.recommender
        active = getattr(recommender, "active", recommender)
        provider = active.romanian_cinema
        self.set_status("Reverific sursele filmelor românești…", True)
        def refresh_sources(progress):
            ids = provider.imdb_ids(refresh=True)
            # Refresh the other trial engine from the shared SQLite snapshot, off the UI thread.
            if ids:
                for engine in (getattr(recommender, "v16", None), getattr(recommender, "v5", None)):
                    other = getattr(engine, "romanian_cinema", None)
                    if other is not None and other is not provider:
                        other.imdb_ids()
            return ids

        worker = WorkerThread(refresh_sources, self)
        self.romanian_refresh_worker = worker

        def success(_ids):
            self.romanian_refresh_worker = None
            self.romanian_result = None
            self.romanian_cache_signature = None
            invalidate = getattr(recommender, "invalidate_round", None)
            if callable(invalidate):
                invalidate("romanian")
            self.set_status("Sursele românești au fost reverificate.", False)
            if self.current_page == "romanian" and not self._ui_closing:
                self.show_page("romanian")

        def failure(message):
            self.romanian_refresh_worker = None
            self.set_status("Reverificarea surselor a eșuat: " + str(message), False)

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.start()

    def page_romanian(self):
        page, content = self.page_shell(
            "Recomandări românești",
            "Selecție personalizată din filme cu limba originală română. Nu trebuie să setezi nimic.",
            [("Recalculează", self._recalculate_romanian, True),
             ("Reverifică sursele", self._refresh_romanian_sources, False)],
        )
        self.romanian_content = content
        if self.catalog_count()[2] <= 0:
            box = QFrame(); box.setObjectName("PremiumCard")
            lay = QVBoxLayout(box); lay.setContentsMargins(20, 18, 20, 18)
            title = QLabel("Catalogul nu este încă pregătit"); title.setObjectName("SectionTitle"); lay.addWidget(title)
            text = QLabel("CineCalendar are nevoie de catalogul IMDb local înainte să poată intersecta filmele românești verificate cu titlurile nevăzute.")
            text.setObjectName("Muted"); text.setWordWrap(True); lay.addWidget(text)
            content.addWidget(box); content.addStretch(1)
            return page

        signature = self._browse_state_signature()
        if (getattr(self, "romanian_cache_signature", None) == signature
                and getattr(self, "romanian_result", None) is not None):
            self._render_romanian(self.romanian_result)
            return page

        content.addWidget(self.loading_panel(
            "Caut filme românești care chiar sunt românești…",
            "Criteriul principal și obligatoriu este limba originală română. România trebuie să apară și ca țară de origine, inclusiv la coproducții. Abia după această verificare ALS + profilul tău decid ordinea.",
        ))
        content.addStretch(1)
        QTimer.singleShot(0, self._load_romanian_async)
        return page

    def _load_romanian_async(self):
        if self._ui_closing or self.current_page != "romanian":
            return
        refresh = getattr(self, "romanian_refresh_worker", None)
        if refresh is not None and refresh.isRunning():
            return
        worker = getattr(self, "romanian_worker", None)
        if worker is not None and worker.isRunning():
            return
        self.set_status("Calculez selecția de cinema românesc…", True)
        signature = self._browse_state_signature()
        worker = WorkerThread(
            lambda progress: self.s.recommender.recommend_romanian(date.today(), count=9),
            self,
        )
        self.romanian_worker = worker

        def success(recs):
            self.romanian_worker = None
            if signature != self._browse_state_signature():
                self.set_status("Datele s-au schimbat; actualizez selecția românească.", False)
                if self.current_page == "romanian":
                    QTimer.singleShot(0, self._load_romanian_async)
                return
            self.romanian_result = list(recs or [])
            self.romanian_cache_signature = signature
            self.set_status("Selecția de cinema românesc este gata.", False)
            if self.current_page == "romanian":
                self._render_romanian(self.romanian_result)
                self._ensure_metadata(self.romanian_result[:6], "romanian")

        def failure(message):
            self.romanian_worker = None
            self.set_status("Selecția de cinema românesc a eșuat.", False)
            if self.current_page == "romanian" and getattr(self, "romanian_content", None) is not None:
                self._clear_layout(self.romanian_content)
                box = QFrame(); box.setObjectName("PremiumCard")
                lay = QVBoxLayout(box); lay.setContentsMargins(20, 18, 20, 18)
                title = QLabel("Nu am putut construi lista acum"); title.setObjectName("SectionTitle"); lay.addWidget(title)
                text = QLabel(message); text.setObjectName("Muted"); text.setWordWrap(True); lay.addWidget(text)
                self.romanian_content.addWidget(box)

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.start()

    def _render_romanian(self, recs):
        content = getattr(self, "romanian_content", None)
        if content is None:
            return
        self._clear_layout(content)

        info = QFrame(); info.setObjectName("PremiumCard")
        il = QHBoxLayout(info); il.setContentsMargins(18, 14, 18, 14); il.setSpacing(12)
        text = QLabel(
            "Criteriu obligatoriu: limba originală este româna. România trebuie să fie și țară de origine/coproducție. Un titlu nu intră doar fiindcă apare România în metadate."
        )
        text.setObjectName("Muted"); text.setWordWrap(True); il.addWidget(text, 1)
        content.addWidget(info)

        status = self.s.recommender.romanian_cinema_status()
        if not recs:
            confirmed = int(status.get("external_count", 0) or 0)
            counts = dict(status.get("diagnostics") or {})
            if status.get("source") == "not-verified":
                message = "Verificarea surselor românești nu a rulat încă."
            elif confirmed == 0:
                message = "Nu există încă titluri confirmate simultan pentru limba originală română și originea România."
            elif "local_catalog_matches" in counts and not int(counts["local_catalog_matches"]):
                message = "Titlurile confirmate nu au corespondent în catalogul IMDb local."
            elif "unseen_catalog_rows" in counts and not int(counts["unseen_catalog_rows"]):
                message = "Titlurile confirmate din catalog sunt deja văzute, evaluate sau respinse."
            else:
                message = "Niciun titlu rămas nu a trecut filtrele de calitate și potrivire personală."
            held = int(status.get("conflict_count", 0) or 0)
            if held:
                message += f" {held:,} titluri au date contradictorii și așteaptă verificare."
            empty = QLabel(message)
            empty.setObjectName("Muted"); empty.setWordWrap(True); content.addWidget(empty)
        else:
            self.record_once(recs[:1], date.today(), "romanian")
            h = QLabel("Alegerea românească pentru tine")
            h.setObjectName("SectionTitle")
            content.addWidget(h)
            content.addWidget(self.compact_recommendation_card(recs[0], 1))
            if len(recs) > 1:
                h2 = QLabel("Alte filme în limba română cu potrivire bună")
                h2.setObjectName("SectionTitle")
                content.addWidget(h2)
                grid = QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(14)
                for i, rec in enumerate(recs[1:], start=2):
                    pos = i - 2
                    grid.addWidget(self.compact_recommendation_card(rec, i), pos // 2, pos % 2)
                wrap = QFrame(); wrap.setLayout(grid); content.addWidget(wrap)

        diagnostic = QFrame(); diagnostic.setObjectName("PremiumCard")
        dl = QVBoxLayout(diagnostic); dl.setContentsMargins(18, 14, 18, 14)
        heading = QLabel("Unde au rămas filmele?"); heading.setObjectName("SectionTitle"); dl.addWidget(heading)
        for line in romanian_diagnostic_lines(status, len(recs)):
            detail = QLabel(line); detail.setObjectName("Muted"); detail.setWordWrap(True); dl.addWidget(detail)
        content.addWidget(diagnostic)

        footer = QFrame(); footer.setObjectName("PremiumCard")
        fl = QVBoxLayout(footer); fl.setContentsMargins(18, 14, 18, 14)
        src = status.get("source", "")
        count = int(status.get("external_count", 0) or 0)
        counts = dict(status.get("source_counts") or {})
        source_labels = {
            "imdb": "IMDb",
            "wikidata": "Wikidata",
            "tmdb": "TMDb",
            "tmdb_details": "detalii TMDb",
            "curated": "catalog RO verificat",
            "verified": "cache verificat",
        }
        source_states = {
            "not-verified": "surse încă neverificate",
            "romanian-multisource-verified": "verificare completă",
            "romanian-multisource-verified-cache": "date verificate păstrate local",
            "romanian-multisource-stale-cache": "date păstrate local; sursele au răspuns parțial",
            "romanian-conflicts-held-for-review": "titlurile contradictorii așteaptă verificare",
            "language-unverified-empty": "fără titluri confirmate în această verificare",
        }
        parts = [
            f"{source_labels.get(key, key)} {int(value):,}"
            for key, value in counts.items()
            if int(value or 0) > 0
        ]
        label = (
            "Sursele urmează să fie verificate; țara locală singură nu este suficientă."
            if src == "not-verified" else
            "Eligibilitatea este verificată din mai multe surse; țara locală singură nu este suficientă."
        )
        if count:
            label = (
                f"Bază strictă: {count:,} identificatori IMDb confirmați ca producții cu limba originală română "
                "și România ca țară de origine."
            )
        if parts:
            label += " Surse: " + " • ".join(parts) + "."
        conflicts = int(status.get("conflict_count", 0) or 0)
        if conflicts:
            label += f" {conflicts:,} titluri cu date contradictorii sunt reținute pentru verificare."
        note = QLabel(label + (f" Stare: {source_states.get(src, 'verificare parțială')}." if src else ""))
        note.setObjectName("Muted"); note.setWordWrap(True); fl.addWidget(note)
        content.addWidget(footer)
        content.addStretch(1)

    window_cls._recalculate_romanian = _recalculate_romanian
    window_cls._refresh_romanian_sources = _refresh_romanian_sources
    window_cls.page_romanian = page_romanian
    window_cls._load_romanian_async = _load_romanian_async
    window_cls._render_romanian = _render_romanian
    window_cls._romanian_cinema_patch_installed = True
