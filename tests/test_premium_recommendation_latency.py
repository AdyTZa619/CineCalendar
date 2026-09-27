from pathlib import Path


def _premium_source() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "cinecalendar"
        / "premium_ui.py"
    ).read_text(encoding="utf-8")


def test_recommendations_render_before_metadata_preflight():
    source = _premium_source()
    start = source.index("def _load_browse_async")
    end = source.index("def _render_browse", start)
    block = source[start:end]

    assert block.index("self._render_browse(self.browse_result[:12])") < block.index(
        'self._ensure_metadata(self.browse_result,"recommendations")'
    )


def test_metadata_does_not_silently_rerank_visible_list():
    source = _premium_source()
    start = source.index("def _ensure_recommendation_metadata")
    end = source.index("def open_details", start)
    block = source[start:end]

    assert 'report["rerank_deferred"]=bool(report.get("ranking_change"))' in block
    assert 'final=list(recs[:12])' in block
    assert 'final=list(self.s.recommender.recommend(' not in block
