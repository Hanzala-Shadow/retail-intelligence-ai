from scripts.run_offline_section_adaptive_rank import section_weight


def test_narrative_sections_use_multiview_rrf():
    assert section_weight("Item_1") == 0.0
    assert section_weight("Item_1A") == 0.0
    assert section_weight("Item_7") == 0.0


def test_financial_statement_notes_use_cross_encoder():
    assert section_weight("Item_8") == 1.0


def test_unknown_sections_use_conservative_equal_blend():
    assert section_weight("Item_2") == 0.5
