from pathlib import Path


def _premium_source() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "cinecalendar"
        / "premium_ui.py"
    ).read_text(encoding="utf-8")


def test_recommendations_render_only_after_bounded_preflight():
    source = _premium_source()
    start = source.index("def _load_browse_async")
    end = source.index("def _render_browse", start)
    block = source[start:end]

    assert "prepare_browse_round(" in block
    assert block.index("prepare_browse_round(") < block.index("self._render_browse(self.browse_result)")


def test_metadata_report_explains_final_rerank_and_fallback():
    source = _premium_source()
    assert "Clasare recalculată după completarea datelor" in source
    assert "Clasarea locală a fost păstrată" in source
    assert "metadatele noi vor intra la următoarea recalculare" not in source
