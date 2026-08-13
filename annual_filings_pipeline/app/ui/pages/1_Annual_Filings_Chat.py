from __future__ import annotations

import uuid

import streamlit as st

from app.ui.api_client import ChatbotApiClient, ChatbotApiError
from app.ui.components import (
    citation_rows, coverage_rows, status_message, technical_metrics,
)

st.set_page_config(
    page_title="Annual Filing Research",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      html, body, [class*="css"] {
        font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
          "Segoe UI", sans-serif;
      }
      .stApp { background: #f7f9fc; color: #172033; }
      [data-testid="stSidebar"] { background: #101827; }
      [data-testid="stSidebar"] * { color: #e8edf7; }
      [data-testid="stSidebar"] .stButton button {
        border: 1px solid #53627a; background: #1a2639; color: #ffffff;
      }
      .research-hero {
        padding: 1.35rem 1.5rem; margin: 0 0 1.1rem 0;
        border: 1px solid #dce3ee; border-radius: 16px;
        background: linear-gradient(135deg, #ffffff 0%, #eef4ff 100%);
        box-shadow: 0 8px 24px rgba(23, 32, 51, 0.06);
      }
      .research-eyebrow {
        color: #3456d1; font-size: .76rem; font-weight: 750;
        letter-spacing: .09em; text-transform: uppercase;
      }
      .research-hero h1 {
        margin: .25rem 0 .25rem; font-size: 2rem; color: #172033;
      }
      .research-hero p { margin: 0; color: #58657a; }
      .answer-label {
        color: #3456d1; font-size: .76rem; font-weight: 750;
        letter-spacing: .07em; text-transform: uppercase;
      }
      [data-testid="stChatMessage"] {
        border: 1px solid #e0e6ef; border-radius: 14px;
        background: #ffffff; padding: .2rem .35rem;
      }
      [data-testid="stExpander"] {
        border: 1px solid #dce3ee; border-radius: 12px; background: #ffffff;
      }
      #MainMenu, footer { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def api_client() -> ChatbotApiClient:
    return ChatbotApiClient()




def safe_filing_markdown(value: object) -> str:
    """Prevent financial dollar amounts from being parsed as LaTeX."""
    return str(value).replace("\\$", "$").replace("$", "\\$")


def render_result(result: dict) -> None:
    level, message = status_message(result)
    getattr(st, level)(message)

    answer = str(result.get("answer") or "").strip()
    if answer:
        st.markdown(
            '<div class="answer-label">Research answer</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            st.markdown(safe_filing_markdown(answer))

    for limitation in result.get("limitations") or []:
        st.warning(str(limitation))

    telemetry = result.get("telemetry") or {}
    if telemetry.get("total_ms") is not None:
        st.caption(
            "Completed in "
            f"{float(telemetry['total_ms']) / 1000:.2f} seconds · "
            f"{int(telemetry.get('evidence_count') or 0)} evidence passages"
        )

    citations = citation_rows(result)
    with st.expander(
        f"Evidence and citations ({len(citations)})",
        expanded=bool(citations),
    ):
        if not citations:
            st.info("No citations were returned for this response.")
        for item in citations:
            with st.container(border=True):
                st.markdown(f"**{item['label']}  ·  {item['source']}**")
                st.caption(
                    f"Accession {item['accession']}  ·  "
                    f"Source chunk {item['chunk']}"
                )
                st.markdown(safe_filing_markdown(item["excerpt"]))

    requirements = coverage_rows(result)
    with st.expander(
        f"Requirement coverage ({len(requirements)})",
        expanded=False,
    ):
        if not requirements:
            st.info("Requirement coverage is unavailable for this response.")
        for item in requirements:
            with st.container(border=True):
                left, right = st.columns([5, 1.25], vertical_alignment="center")
                left.markdown(f"**{item['requirement']}**")
                left.caption(item["scope"])
                if item["supported"]:
                    right.success(item["status"])
                else:
                    right.warning(item["status"])

    if st.session_state.get("show_technical"):
        with st.expander("Technical panel", expanded=False):
            st.caption(
                "Operational diagnostics for this request. Routing-catalog "
                "preload is a one-time API startup measurement."
            )
            legacy = {"Retrieval seconds", "Generation seconds", "Total seconds"}
            metrics = [
                (label, value if value is not None else "Unavailable")
                for label, value in technical_metrics(result).items()
                if label not in legacy
            ]
            for offset in range(0, len(metrics), 2):
                columns = st.columns(2)
                for column, (label, value) in zip(
                    columns, metrics[offset:offset + 2]
                ):
                    column.metric(label, value)


st.markdown(
    """
    <div class="research-hero">
      <div class="research-eyebrow">Retail Intelligence · SEC 10-K corpus</div>
      <h1>Annual Filing Research</h1>
      <p>Evidence-grounded company analysis with filing-level citations.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("## Research workspace")
    st.caption("Indexed annual filings only · Public-web search disabled")
    health_slot = st.empty()
    st.divider()
    request_pending = bool(st.session_state.get("pending_question"))
    st.toggle(
        "Show technical details",
        key="show_technical",
        disabled=request_pending,
    )
    if st.button(
        "Clear conversation",
        use_container_width=True,
        disabled=request_pending,
    ):
        st.session_state.messages = []
        st.session_state.pending_question = None
        st.session_state.conversation_id = str(uuid.uuid4())
        st.rerun()
    st.divider()
    st.markdown("**Good questions include**")
    st.caption(
        "A company or ticker, one or more filing years, and a focused topic "
        "such as sales, margins, risks, liquidity, or business operations."
    )

if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = str(uuid.uuid4())

api_ready = False
try:
    health = api_client().health()
    api_ready = health.get("status") == "ready"
    if api_ready:
        health_slot.success("System ready")
    else:
        health_slot.warning("System warming up")
        st.info(
            "The research service is starting or waiting for a retrieval "
            "component. Existing responses remain visible."
        )
except ChatbotApiError:
    health_slot.error("Service unavailable")
    st.error("The research service cannot currently be reached. Please retry shortly.")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_result(message["result"])


pending_question = st.session_state.get("pending_question")

if pending_question:
    with st.chat_message("assistant"):
        try:
            with st.status(
                "Preparing filing analysis",
                expanded=True,
            ) as status:
                st.write("Resolving companies, filing years, and research scope")
                st.write("Retrieving, balancing, and ranking filing evidence")
                result = api_client().chat(
                    pending_question,
                    conversation_id=st.session_state.conversation_id,
                )
                label = (
                    "Clarification required"
                    if result.get("status") == "ambiguous_request"
                    else "Analysis complete"
                )
                status.update(
                    label=label,
                    state="complete",
                    expanded=False,
                )
            st.session_state.messages.append({
                "role": "assistant",
                "result": result,
            })
        except ChatbotApiError as error:
            st.session_state.messages.append({
                "role": "assistant",
                "result": {
                    "status": "request_failed",
                    "answer": None,
                    "limitations": [str(error)],
                    "citations": [],
                    "requirements": [],
                },
            })
        finally:
            st.session_state.pending_question = None
    st.rerun()

question = st.chat_input(
    "Ask about one or more companies, filing years, or topics",
    disabled=(
        not api_ready
        or bool(st.session_state.get("pending_question"))
    ),
)

if question:
    st.session_state.messages.append({
        "role": "user",
        "content": question,
    })
    st.session_state.pending_question = question
    st.rerun()
