from __future__ import annotations

from datetime import date

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

from .qt_ui import WorkerThread


def install_romanian_cinema_ui_patch(window_cls) -> None:
    """Add a dedicated, precision-first Romanian cinema page."""
    if getattr(window_cls, "_romanian_cinema_patch_installed", False):
        return

    nav = list(window_cls.NAV)
    if not any(key == "romanian" for key, _label in nav):
        position = next((i + 1 for i, (key, _label) in enumerate(nav) if key == "recommendations"), 2)
        nav.insert(position, ("romanian", "Recomandări românești"))
        window_cls.NAV = nav

    def page_romanian(self):
        page, content = self.page_shell(
            "Recomandări românești",
            "Selecție personalizată din filme cu limba originală română. Nu trebuie să setezi nimic.",
            [("Recalculează", lambda: self.show_page("romanian"), True)],
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

        content.addWidget(self.loading_panel(
            "Caut filme românești care chiar sunt românești…",
            "Criteriul principal și obligatoriu este limba originală română. România trebuie să apară și ca țară de origine, inclusiv la coproducții. Abia după această verificare ALS + profilul tău decid ordinea.",
        ))
        content.addStretch(1)
        QTimer.singleShot(0, self._load_romanian_async)
        return page

    def _load_romanian_async(self):
        worker = getattr(self, "romanian_worker", None)
        if worker is not None and worker.isRunning():
            return
        self.set_status("Calculez selecția de cinema românesc…", True)
        worker = WorkerThread(
            lambda progress: self.s.recommender.recommend_romanian(date.today(), count=9),
            self,
        )
        self.romanian_worker = worker

        def success(recs):
            self.romanian_worker = None
            self.romanian_result = list(recs or [])
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

        if not recs:
            empty = QLabel("Nu am găsit momentan suficiente filme nevăzute cu limba originală română care să treacă și pragul de încredere al recomandării.")
            empty.setObjectName("Muted"); empty.setWordWrap(True); content.addWidget(empty); content.addStretch(1)
            return

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

        status = self.s.recommender.romanian_cinema_status()
        footer = QFrame(); footer.setObjectName("PremiumCard")
        fl = QVBoxLayout(footer); fl.setContentsMargins(18, 14, 18, 14)
        src = status.get("source", "")
        count = int(status.get("external_count", 0) or 0)
        label = "Lista de eligibilitate verificată după limba originală este memorată local și se actualizează rar."
        if count:
            label = f"Bază strictă de eligibilitate: {count:,} identificatori IMDb verificați cu limba originală română și România ca țară de origine."
        note = QLabel(label + (f" Sursă curentă: {src}." if src else ""))
        note.setObjectName("Muted"); note.setWordWrap(True); fl.addWidget(note)
        content.addWidget(footer)
        content.addStretch(1)

    window_cls.page_romanian = page_romanian
    window_cls._load_romanian_async = _load_romanian_async
    window_cls._render_romanian = _render_romanian
    window_cls._romanian_cinema_patch_installed = True
