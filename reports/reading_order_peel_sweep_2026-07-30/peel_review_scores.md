# Panel-peel human scoring sheet

Date: 2026-07-30 | Reviewer: Aziz | Scored: 19 of 19

Compared **as shipped** against **peel disabled**.

| # | Page | Regions (off -> on) | Status (off -> on) | Score | Note |
|---|---|---|---|---|---|
| 1 | JILL|JILL-J.Jill-2024.pdf|18 | 5 -> 4 | candidate_ready -> candidate_ready | **Better** |  |
| 2 | FOSL|FOSL-FOSSIL GROUP INC-2021.pdf|32 | 7 -> 5 | candidate_ready -> candidate_ready | **Better** |  |
| 3 | CVS|CVS-CVS HEALTH CORP-2020.pdf|28 | 3 -> 3 | candidate_ready -> candidate_ready | **Better** |  |
| 4 | RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|45 | 4 -> 5 | candidate_ready -> candidate_ready | **Better** |  |
| 5 | LESL|LESL-LESLIE'S INC-2021.pdf|13 | 3 -> 5 | candidate_ready -> candidate_ready | **Better** |  |
| 6 | TGT|TGT-TARGET CORP-2019.pdf|14 | 7 -> 10 | candidate_ready -> candidate_ready | **Better** | partial: crosses from one column to the other mid-column, then continues; still better |
| 7 | ACI|ACI-ALBERTSONS COS INC-2022.pdf|13 | 5 -> 9 | candidate_ready -> candidate_ready | **Better** | partial: still joins two columns into one line (TALENT / MEANINGFUL DEVELOPMENT METRICS run together) |
| 8 | DKS|DKS-DICKS SPORTING GOODS INC-2018.pdf|75 | 3 -> 8 | candidate_ready -> candidate_ready | **Same** |  |
| 9 | KSS|KSS-KOHL'S-2024.pdf|22 | 1 -> 7 | candidate_ready -> candidate_ready | **Worse** | sidebar OPERATIONAL INITIATIVES moved into the middle, splitting HOW2RECYCLE from BRANDED APPAREL; blocks intact |
| 10 | JILL|JILL-J.Jill-2024.pdf|35 | 4 -> 11 | candidate_ready -> candidate_ready | **Same** | table |
| 11 | TGT|TGT-TARGET CORP-2019.pdf|8 | 3 -> 11 | candidate_ready -> candidate_ready | **Better** |  |
| 12 | BBWI|BBWI-BATH & BODY WORKS INC-2023.pdf|69 | 2 -> 2 | candidate_ready -> candidate_ready | **Same** |  |
| 13 | FOSL|FOSL-FOSSIL GROUP INC-2021.pdf|33 | 6 -> 6 | candidate_ready -> candidate_ready | **Better** |  |
| 14 | CASY|CASY-CASEYS GENERAL STORES INC-2022.pdf|18 | 4 -> 4 | candidate_ready -> candidate_ready | **Better** |  |
| 15 | RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|34 | 3 -> 3 | candidate_ready -> candidate_ready | **Better** |  |
| X1 | JWN|JWN-NORSTROM-2024.pdf|5 | 2 -> 6 | needs_review -> candidate_ready | **Better** | the only status flip; three pillar panels separated correctly |
| X2 | ACI|ACI-ALBERTSONS COS INC-2022.pdf|29 | 1 -> 9 | candidate_ready -> candidate_ready | **Worse** | table; 2022 PROGRESS lands mid-table between DEI and WASTE REDUCTION; blocks intact |
| X3 | ACI|ACI-ALBERTSONS COS INC-2022.pdf|30 | 5 -> 13 | candidate_ready -> candidate_ready | **Better** | table; usually both configurations are poor on tables |
| X4 | KR|KR-KROGER CO-2023.pdf|42 | 4 -> 9 | candidate_ready -> candidate_ready | **Better** |  |

## Tally

- Better: 14
- Same: 3
- Worse: 2
- **Net (Better - Worse): +12**

## Severity of the two Worse pages

Both are block-ORDER regressions (severity 3), not sentence shredding (severity 1).
Verified by comparing block sequence in both configurations:

- `KSS p22`: off = Packaging, HOW2RECYCLE, BRANDED APPAREL, OPERATIONAL INITIATIVES;
  on = Packaging, HOW2RECYCLE, **OPERATIONAL INITIATIVES**, BRANDED APPAREL.
- `ACI p29`: off = all four topic goals then 2022 PROGRESS;
  on = CLIMATE, DEI, **2022 PROGRESS**, WASTE REDUCTION, COMMUNITY.

No page had the peel create interleaving that was not already there. The pre-registered
disqualifying condition did not trigger.

## Pattern in the notes

The reviewer flagged tables specifically on rows 10, X2 and X3 (Same / Worse / Better),
and partial fixes on rows 6 and 7 (better, but columns still join).

- Multi-panel and multi-column PROSE pages: clear win.
- Dense TABLES: neutral to negative. Both configurations are usually poor; the peel
  rearranges the failure rather than fixing it. Row structure, not panel geometry, is
  what those pages need.
