from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Retail Intelligence",
    page_icon="📑",
    layout="wide",
)

st.markdown(
    """
    <style>
      html, body, [class*="css"] {
        font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
          "Segoe UI", sans-serif;
      }
      .stApp { background: #f7f9fc; color: #172033; }
      .landing-hero {
        padding: 2rem; border: 1px solid #dce3ee; border-radius: 18px;
        background: linear-gradient(135deg, #ffffff 0%, #eaf1ff 100%);
        box-shadow: 0 10px 30px rgba(23, 32, 51, .07);
      }
      .landing-kicker {
        color: #3456d1; font-size: .78rem; font-weight: 750;
        letter-spacing: .1em; text-transform: uppercase;
      }
      .landing-hero h1 { color: #172033; margin: .35rem 0; font-size: 2.35rem; }
      .landing-hero p { color: #58657a; font-size: 1.03rem; margin-bottom: 0; }
      #MainMenu, footer { visibility: hidden; }
    </style>
    <div class="landing-hero">
      <div class="landing-kicker">Retail Intelligence Platform</div>
      <h1>Annual Filing Research</h1>
      <p>Ask focused questions across indexed SEC 10-K filings and receive
      evidence-grounded answers with traceable source citations.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.write("")
left, right = st.columns([1.35, 1])
with left:
    st.markdown("### Research capabilities")
    st.markdown(
        """
        - Compare companies or filing years using a consistent evidence pipeline.
        - Investigate sales, margins, liquidity, risks, strategy, and operations.
        - Review the exact filing excerpts supporting each answer.
        - Inspect requirement coverage and optional technical diagnostics.
        """
    )
with right:
    with st.container(border=True):
        st.markdown("### Corpus boundaries")
        st.write(
            "Answers use indexed annual filings only; public-web search is disabled."
        )
        st.caption(
            "Source-based informational research—not investment, legal, tax, "
            "or accounting advice."
        )

st.page_link(
    "pages/1_Annual_Filings_Chat.py",
    label="Open research workspace",
    icon="📄",
    use_container_width=True,
)
