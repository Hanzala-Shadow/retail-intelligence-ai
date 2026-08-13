from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Annual Filings Intelligence",
    page_icon="📑",
    layout="wide",
)

st.title("Annual Filing Research")
st.subheader("Evidence-grounded analysis from indexed SEC filings")

st.markdown(
    """
Enter a focused question using company names or tickers and the relevant filing
year or years. Single-company, cross-company, and multi-year analysis are
supported. Each response includes traceable source citations.
"""
)

left, right = st.columns([3, 2])
with left:
    st.markdown("### Suitable questions")
    st.markdown(
        """
- Compare the disclosed inventory policies of two companies for a specified year.
- How did a retailer describe changes in its store footprint across two filings?
- What principal risks did a company disclose in Item 1A?
- Summarize a company’s business model and stakeholder value proposition.
"""
    )
with right:
    st.info(
        "Answers use the project’s indexed annual filings. "
        "The chatbot does not search the public web."
    )
    st.warning(
        "This tool provides source-based informational analysis, not investment, "
        "legal, tax, or accounting advice."
    )

st.page_link(
    "pages/1_Annual_Filings_Chat.py",
    label="Open research workspace",
    icon="📄",
)
