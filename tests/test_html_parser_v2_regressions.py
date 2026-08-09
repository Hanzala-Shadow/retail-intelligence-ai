from src.html_parser_v2 import parse_html


def test_inline_words_and_repeated_toc_headers(tmp_path):
    source = tmp_path / "inline_fragments.htm"
    source.write_text(
        """
        <html><body>
          <div>Table of Contents</div>
          <p>Introductory narrative.</p>
          <div>Table of Contents</div>
          <p>Gro<span>ss</span> profit improved.</p>
          <p>Results were c<span>ompared</span> with last year.</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    text, semantic, layout, flags = parse_html(source)

    assert text.count("Table of Contents") == 1
    assert "Gross profit improved." in text
    assert "Results were compared with last year." in text
    assert "Gro\nss" not in text
    assert "Gro ss" not in text
    assert semantic == 0
    assert layout == 0
    assert flags == ["suspiciously_thin_parse"]


def test_zero_font_layout_parent_preserves_visible_descendants(tmp_path):
    from src.html_parser_v2 import parse_html

    source = tmp_path / "workiva.xhtml"
    source.write_text(
        """<?xml version="1.0" encoding="ASCII"?>
        <html xmlns="http://www.w3.org/1999/xhtml">
          <body>
            <div style="font-size:0;height:792pt">
              <span style="font-size:10pt">Item 1. Business</span>
              <div style="font-size:10pt">
                Visible company-specific operating disclosure.
              </div>
              <span style="font-size:0">HIDDEN ZERO FONT</span>
            </div>
          </body>
        </html>
        """,
        encoding="ascii",
    )

    parsed, _, _, _ = parse_html(source)

    assert "Item 1. Business" in parsed
    assert "Visible company-specific operating disclosure" in parsed
    assert "HIDDEN ZERO FONT" not in parsed
