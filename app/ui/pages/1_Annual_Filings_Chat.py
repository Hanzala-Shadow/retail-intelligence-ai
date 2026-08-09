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
)


@st.cache_resource
def api_client() -> ChatbotApiClient:
    return ChatbotApiClient()


def render_result(result: dict) -> None:
    level, message = status_message(result)
    getattr(st, level)(message)
    if result.get("answer"):
        st.markdown(result["answer"])
    for limitation in result.get("limitations") or []:
        st.warning(limitation)

    telemetry = result.get("telemetry") or {}
    if telemetry.get("total_ms") is not None:
        st.caption(
            f"Completed in {float(telemetry['total_ms']) / 1000:.2f} seconds"
        )

    with st.expander("Evidence and citations", expanded=True):
        citations = citation_rows(result)
        if not citations:
            st.info("No citations were returned for this response.")
        for item in citations:
            with st.container(border=True):
                st.markdown(f"#### {item['label']}  ·  {item['source']}")
                st.caption(
                    f"Accession {item['accession']}  |  Source chunk {item['chunk']}"
                )
                st.markdown("**Filing excerpt**")
                st.write(item["excerpt"])

    with st.expander("Requirement coverage", expanded=False):
        requirements = coverage_rows(result)
        if not requirements:
            st.info("Requirement coverage is unavailable for this response.")
        for item in requirements:
            with st.container(border=True):
                left, right = st.columns([4, 1])
                left.markdown(f"**{item['requirement']}**")
                left.caption(item["scope"])
                if item["supported"]:
                    right.success(item["status"])
                else:
                    right.warning(item["status"])

    if st.session_state.get("show_technical"):
        with st.expander("Technical panel", expanded=True):
            st.caption(
                "Operational timings and model usage for this request. "
                "The routing catalog preload is a one-time API startup cost."
            )
            legacy_labels = {"Retrieval seconds", "Generation seconds", "Total seconds"}
            for label, value in technical_metrics(result).items():
                if label in legacy_labels:
                    continue
                st.markdown(f"**{label}:** {value if value is not None else 'Unavailable'}")
            st.markdown("**GPU device:** Remote CUDA (Tesla T4)")


st.title("Annual Filing Research")
st.caption("Source-grounded analysis of indexed SEC annual filings")

with st.sidebar:
    st.markdown("### Research session")
    st.success("Live data")
    st.caption("Indexed filings only. Public-web search is disabled.")
    st.toggle("Show technical panel", key="show_technical")
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation_id = str(uuid.uuid4())
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = str(uuid.uuid4())

try:
    health = api_client().health()
    if health.get("status") != "ready":
        st.warning("The chatbot API is not ready.")
except ChatbotApiError:
    st.error("The chatbot service is currently unavailable.")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_result(message["result"])

question = st.chat_input("Ask about one or more companies, filing years, or topics")
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            with st.status("Preparing filing analysis", expanded=True) as status:
                st.write("Resolving companies, filing years, and requested scope")
                st.write("Retrieving and ranking filing evidence")
                result = api_client().chat(
                    question,
                    conversation_id=st.session_state.conversation_id,
                )
                label = (
                    "Clarification required"
                    if result.get("status") == "ambiguous_request"
                    else "Analysis complete"
                )
                status.update(label=label, state="complete")
            render_result(result)
            st.session_state.messages.append({"role": "assistant", "result": result})
        except ChatbotApiError as error:
            st.error(str(error))
