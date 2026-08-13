from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from html_parser_v2 import parse_html


def test_parser_removes_hidden_content_and_classifies_tables(tmp_path):
    source = tmp_path / "sample.htm"
    source.write_text(
        """
        <html><body>
          <div style="display:none">HIDDEN SECRET</div>
          <ix:hidden>HIDDEN XBRL</ix:hidden>
          <h1>Item 1. Business</h1>
          <p>Visible operating narrative.</p>
          <table>
            <tr><td>Item 1</td><td>3</td></tr>
            <tr><td>Item 1A</td><td>8</td></tr>
            <tr><td>Item 7</td><td>30</td></tr>
          </table>
          <table>
            <tr><th>Year ended</th><th>2025</th><th>2024</th></tr>
            <tr><td>Net sales</td><td>$100</td><td>$90</td></tr>
            <tr><td>Gross profit</td><td>$40</td><td>$35</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    text, semantic, layout, flags = parse_html(source)
    assert "HIDDEN SECRET" not in text
    assert "HIDDEN XBRL" not in text
    assert semantic == 1
    assert layout == 1
    assert text.count("[TABLE_START:") == 1
    assert "Net sales | $100 | $90" in text
    assert flags == ["suspiciously_thin_parse"]
