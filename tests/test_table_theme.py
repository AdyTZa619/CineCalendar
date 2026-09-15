from cinecalendar.table_theme_patch import table_qss


def test_dark_table_theme_never_uses_native_white_alternate_rows():
    qss = table_qss(True)
    assert "alternate-background-color:#1B1F29" in qss
    assert "background-color:#151820" in qss
    assert "color:#F7F7F4" in qss
    assert "QHeaderView::section" in qss
    assert "selection-background-color:#315FD6" in qss


def test_light_table_theme_has_explicit_readable_palette():
    qss = table_qss(False)
    assert "alternate-background-color:#F0EEE9" in qss
    assert "background-color:#FFFFFF" in qss
    assert "color:#17181C" in qss
