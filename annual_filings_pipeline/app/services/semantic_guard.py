"""Additional request-local factual constraints that preserve SYSTEM identity."""

SIDEDNESS_INSTRUCTION = (
    "\n- Do not characterize a platform or ecosystem as one-sided, two-sided, "
    "three-sided, four-sided, multi-sided, or with any other side count unless "
    "that exact characterization appears verbatim in the supplied evidence. "
    "Describe the evidenced participant groups directly instead.\n"
)


def guarded_prompt(prompt: str) -> str:
    marker = "- Return only the final answer with canonical citations."
    if marker not in prompt:
        raise ValueError("generation prompt contract marker is missing")
    return prompt.replace(marker, SIDEDNESS_INSTRUCTION + marker)
