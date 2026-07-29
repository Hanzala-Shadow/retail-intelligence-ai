"""Figures for the ESG Database Audit Report (2026 Q3).

Palette values are taken verbatim from the dataviz skill's documented reference
instance (references/palette.md): categorical slots 1-2 and the light-mode
ordinal blue ramp (no step lighter than 250). Light surface only - the output
is a printed DOCX, so no dark mode is generated.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import numpy as np
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The ESG pipeline moved under esg/; these reports live outside both
# pipelines, so they name the two entries a pipeline _bootstrap would add.
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[1] / "esg"))

import config  # noqa: E402

R = config.REFERENCE_DIR.as_posix() + "/"
OUT = (HERE / "figs").as_posix() + "/"   # this script writes beside itself

# --- documented palette values -------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e6e5e1"
S1 = "#2a78d6"        # categorical slot 1 (blue)
S2 = "#eb6834"        # categorical slot 2 (orange)
ORD = ["#86b6ef", "#2a78d6", "#104281"]   # ordinal ramp, light floor = step 250
MUTED = "#b7d3f6"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans", "font.size": 9,
    "text.color": INK, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.edgecolor": GRID, "axes.linewidth": 0.8,
    "xtick.major.size": 0, "ytick.major.size": 0,
    "axes.grid": False, "legend.frameon": False,
})


def frame(ax, xgrid=False, ygrid=True):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    if ygrid:
        ax.yaxis.grid(True, color=GRID, lw=0.8, ls="-")
    if xgrid:
        ax.xaxis.grid(True, color=GRID, lw=0.8, ls="-")
    ax.set_axisbelow(True)


def save(fig, name):
    fig.savefig(OUT + name, dpi=200, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    print("wrote", name)


# --- data -----------------------------------------------------------------
ch = pd.read_csv(R + "esg_chunks_index_enriched.csv", low_memory=False)
vm = pd.read_csv(R + "vector_index_manifest.csv", low_memory=False,
                 usecols=["chunk_id", "eligibility_decision", "retrieval_state",
                          "layout_qa_status"])
m = ch.merge(vm, on="chunk_id", how="left")
xl = pd.ExcelFile(R + "apparel_footwear_v3.xlsx")
rr = (xl.parse("retail").dropna(subset=["NRF Rank"])
        .drop_duplicates("(tic) Ticker Symbol")
        .set_index("(tic) Ticker Symbol")["NRF Rank"])
m["nrf"] = m.canonical_ticker.map(rr)
m["size_class"] = np.where(m.nrf.notna(), "NRF Top-100 retailer", "Outside NRF Top-100")
pq = pd.read_csv(R + "esg_page_layout_qa.csv", low_memory=False)

# =========================================================================
# Fig 1 - chunk token-size distribution
# =========================================================================
fig, ax = plt.subplots(figsize=(6.6, 3.0))
ax.hist(m.token_count, bins=np.arange(0, 625, 25), color=S1, edgecolor=SURFACE, lw=1.2)
frame(ax)
ax.axvline(100, color=INK2, lw=1.0)
ax.axvline(600, color=INK2, lw=1.0)
ax.text(103, ax.get_ylim()[1] * 0.94, "policy band 100-600 tokens",
        fontsize=8, color=INK2, va="top")
ax.set_xlabel("Tokens per chunk")
ax.set_ylabel("Chunks")
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}"))
ax.set_title("Chunk size distribution — 96.5% inside the 100–600 token retrieval band",
             fontsize=10, pad=10, loc="left")
save(fig, "fig1_token_distribution.png")

# =========================================================================
# Fig 2 - chunks by report year
# =========================================================================
yr = m.report_year.value_counts().sort_index()
yr = yr[(yr.index >= 2014) & (yr.index <= 2025)]
fig, ax = plt.subplots(figsize=(6.6, 2.9))
ax.bar(yr.index, yr.values, color=S1, width=0.62)
frame(ax)
ax.set_xlabel("Report year")
ax.set_ylabel("Chunks")
ax.set_xticks(list(yr.index))
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}"))
peak = yr.idxmax()
ax.annotate(f"{yr.max():,}", xy=(peak, yr.max()), xytext=(0, 4),
            textcoords="offset points", ha="center", fontsize=8, color=INK)
ax.set_title("Temporal depth — 12 consecutive reporting years, mass concentrated in 2020–2025",
             fontsize=10, pad=10, loc="left")
save(fig, "fig2_year_distribution.png")

# =========================================================================
# Fig 3 - ESG theme coverage
# =========================================================================
sec = m.section_code.value_counts().drop(labels=["full_document"], errors="ignore").head(14)
labels = {
    "human_capital": "Human capital", "supply_chain_ethics": "Supply chain & ethics",
    "environmental": "Environment", "community": "Community",
    "waste": "Waste & circularity", "diversity_equity_inclusion": "DEI",
    "about_this_report": "About this report", "governance": "Governance",
    "emissions": "Emissions (GHG)", "ethics_compliance": "Ethics & compliance",
    "energy": "Energy", "appendix": "Appendix / indices", "water": "Water",
    "climate": "Climate strategy",
}
sec = sec.sort_values()
fig, ax = plt.subplots(figsize=(6.6, 4.0))
ax.barh([labels.get(i, i) for i in sec.index], sec.values, color=S1, height=0.66)
frame(ax, xgrid=True, ygrid=False)
ax.set_xlabel("Chunks")
ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}"))
for y, v in enumerate(sec.values):
    ax.text(v + 90, y, f"{v:,}", va="center", fontsize=8, color=INK2)
ax.set_xlim(0, sec.max() * 1.14)
ax.set_title("Thematic coverage — every core ESG disclosure area is populated",
             fontsize=10, pad=10, loc="left")
save(fig, "fig3_theme_coverage.png")

# =========================================================================
# Fig 4 - big vs small parity (2 series, same % axis)
# =========================================================================
def rates(d):
    return pd.Series({
        "Narrative-grade\nchunks": (d.chunk_quality_tier == "narrative").mean() * 100,
        "Retrieval-eligible\nchunks": (d.eligibility_decision == "eligible").mean() * 100,
        "Citation-verified\nchunks": (d.citation_validation_status == "verified_exact").mean() * 100,
        "Tokens inside\npolicy band": d.token_count.between(100, 600).mean() * 100,
    })

par = m.groupby("size_class").apply(rates, include_groups=False)
order = ["NRF Top-100 retailer", "Outside NRF Top-100"]
par = par.loc[order]
x = np.arange(par.shape[1])
w = 0.36
fig, ax = plt.subplots(figsize=(6.6, 3.2))
ax.bar(x - w / 2 - 0.01, par.iloc[0], w, label=order[0], color=S1)
ax.bar(x + w / 2 + 0.01, par.iloc[1], w, label=order[1], color=S2)
frame(ax)
ax.set_xticks(x)
ax.set_xticklabels(par.columns, fontsize=8)
ax.set_ylabel("% of the company group's chunks")
ax.set_ylim(0, 116)
ax.set_yticks([0, 25, 50, 75, 100])
for i in range(par.shape[1]):
    for j, off in enumerate([-w / 2 - 0.01, w / 2 + 0.01]):
        ax.text(i + off, par.iloc[j, i] + 2, f"{par.iloc[j, i]:.0f}",
                ha="center", fontsize=8, color=INK)
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.20), ncol=2, fontsize=8.5)
ax.set_title("Quality parity — large and small issuers are processed to the same standard",
             fontsize=10, pad=10, loc="left")
save(fig, "fig4_size_parity.png")

# =========================================================================
# Fig 5 - quality tier composition by size class (ordinal ramp)
# =========================================================================
tier = (m.groupby(["size_class", "chunk_quality_tier"]).size()
          .unstack(fill_value=0).loc[order])
tier = tier[["narrative", "layout_sensitive", "noise"]]
tpc = tier.div(tier.sum(axis=1), axis=0) * 100
names = ["Narrative", "Layout-sensitive", "Boilerplate / noise"]
fig, ax = plt.subplots(figsize=(6.6, 2.1))
left = np.zeros(len(tpc))
for k, (c, col) in enumerate(zip(tpc.columns, ORD)):
    ax.barh(tpc.index, tpc[c], left=left, color=col, height=0.5, label=names[k])
    for y, (v, l) in enumerate(zip(tpc[c], left)):
        if v > 6:
            ax.text(l + v / 2, y, f"{v:.1f}%", ha="center", va="center",
                    fontsize=8, color="#ffffff" if k else INK)
    left = left + tpc[c].values
frame(ax, ygrid=False)
ax.set_xlim(0, 100)
ax.set_xlabel("% of chunks")
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.34), ncol=3, fontsize=8.5)
ax.set_title("Narrative quality mix — three chunks in four are clean prose",
             fontsize=10, pad=10, loc="left")
save(fig, "fig5_quality_tier.png")

# =========================================================================
# Fig 6 - page-level layout disposition (ordinal)
# =========================================================================
d = pq.decision.value_counts()
groups = pd.Series({
    "Clean\nsingle-column": d.get("auto_pass", 0),
    "Column order\nrebuilt": d.get("auto_pass_column_order_reconstructed", 0),
    "Region order\nrebuilt": d.get("auto_pass_region_order_reconstructed", 0),
    "Table extraction\nverified": d.get("auto_pass_verified_table_extraction", 0),
    "Navigation /\ncontents": d.get("auto_pass_navigation_contents", 0),
    "Held for review\n(fail-closed)": d.get("auto_hold", 0),
})
# One hue for every cleared disposition; the accent marks the single exception.
cols = [S1] * 5 + [S2]
fig, ax = plt.subplots(figsize=(6.6, 3.1))
ax.bar(range(len(groups)), groups.values, color=cols, width=0.62)
frame(ax)
ax.set_xticks(range(len(groups)))
ax.set_xticklabels(groups.index, fontsize=7.5)
ax.set_ylabel("Pages")
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}"))
tot = groups.sum()
for i, v in enumerate(groups.values):
    ax.text(i, v + 300, f"{v:,}\n{v/tot*100:.1f}%", ha="center", fontsize=7.5, color=INK2)
ax.set_ylim(0, groups.max() * 1.26)
ax.set_title("Layout adjudication across all 42,378 pages — 87.6% cleared, 12.4% held by design",
             fontsize=10, pad=10, loc="left")
save(fig, "fig6_layout_decisions.png")

# =========================================================================
# Fig 7 - retrieval eligibility waterfall
# =========================================================================
steps = [
    ("Chunks\nproduced", len(m), S1),
    ("Held for VLM\n(layout)", -(m.retrieval_state == "held_for_vlm").sum(), MUTED),
    ("Held for doc\nreview", -(m.retrieval_state == "held_for_document_review").sum(), MUTED),
    ("Duplicates\nexcluded", -(m.retrieval_state == "excluded_duplicate").sum(), MUTED),
    ("Eligible\ntoday", (m.eligibility_decision == "eligible").sum(), S1),
]
fig, ax = plt.subplots(figsize=(6.6, 3.1))
run = 0
tops = []
for i, (lab, val, col) in enumerate(steps):
    if i == 0 or i == len(steps) - 1:
        ax.bar(i, abs(val), bottom=0, color=col, width=0.6)
        run = abs(val)
        ax.text(i, abs(val) + 1100, f"{abs(val):,}", ha="center", fontsize=8.5, color=INK)
    else:
        ax.bar(i, max(abs(val), 60), bottom=run + val, color=col, width=0.6)
        ax.text(i, run + 1100, f"{val:,}", ha="center", fontsize=8.5, color=INK2)
        run = run + val
    tops.append(run)
# connector rules between steps, so the running total is readable
for i in range(len(steps) - 1):
    ax.plot([i + 0.30, i + 1 - 0.30], [tops[i], tops[i]], color=GRID, lw=1.0, zorder=0)
frame(ax)
ax.set_xticks(range(len(steps)))
ax.set_xticklabels([s[0] for s in steps], fontsize=8)
ax.set_ylabel("Chunks")
ax.set_ylim(0, len(m) * 1.14)
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}"))
ax.set_title("From produced to retrievable — 77.4% eligible now, the remainder withheld, not lost",
             fontsize=10, pad=10, loc="left")
save(fig, "fig7_eligibility_waterfall.png")

# =========================================================================
# Fig 8 - corpus footprint by company (top 20)
# =========================================================================
pc = m.groupby("canonical_ticker").agg(chunks=("chunk_id", "size"))
pc["nrf"] = pc.index.map(rr)
top = pc.sort_values("chunks", ascending=False).head(20).sort_values("chunks")
cols = [S1 if pd.notna(v) else S2 for v in top.nrf]
fig, ax = plt.subplots(figsize=(6.6, 4.2))
ax.barh(top.index, top.chunks, color=cols, height=0.66)
frame(ax, xgrid=True, ygrid=False)
ax.set_xlabel("Chunks")
for y, v in enumerate(top.chunks):
    ax.text(v + 22, y, f"{v:,}", va="center", fontsize=8, color=INK2)
ax.set_xlim(0, top.chunks.max() * 1.14)
h = [plt.Rectangle((0, 0), 1, 1, color=S1), plt.Rectangle((0, 0), 1, 1, color=S2)]
ax.legend(h, ["NRF Top-100 retailer", "Outside NRF Top-100"],
          loc="lower right", fontsize=8.5)
ax.set_title("Top 20 issuers by corpus footprint — depth is not confined to the largest names",
             fontsize=10, pad=10, loc="left")
save(fig, "fig8_top_companies.png")

print("\nAll figures written to", OUT)
