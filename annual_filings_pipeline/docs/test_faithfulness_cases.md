# Test Case 1 — GOOD answer (should score high, no hallucination)

**QUESTION:** How does Amazon describe risks related to artificial intelligence in its 10-K?

**RETRIEVED CHUNKS:**
> "We face significant risks from the rapid development of artificial intelligence by our
> competitors, which could disrupt our business operations and market position."
>
> "Public perceptions regarding social or ethical issues related to our development and use
> of artificial intelligence may affect our reputation and the acceptance of our products
> and services."

**GENERATED ANSWER:**
"Amazon states that it faces significant risks from competitors' rapid development of
artificial intelligence, which could disrupt its business operations and market position.
The company also notes that public perceptions around the social and ethical issues of AI
could affect its reputation and how customers accept its products and services."

---

# Test Case 2 — HALLUCINATED answer (should score low, hallucination flagged)

**QUESTION:** How does Amazon describe risks related to artificial intelligence in its 10-K?

**RETRIEVED CHUNKS:**
> "We face significant risks from the rapid development of artificial intelligence by our
> competitors, which could disrupt our business operations and market position."
>
> "Public perceptions regarding social or ethical issues related to our development and use
> of artificial intelligence may affect our reputation and the acceptance of our products
> and services."

**GENERATED ANSWER:**
"Amazon states that it faces significant risks from competitors' rapid development of
artificial intelligence. The company disclosed that it invested $14.2 billion in AI research
in 2023 alone, and identified over 200 potential AI-related lawsuits as a material risk to
its operations."
