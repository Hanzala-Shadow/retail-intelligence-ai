# Region-level verified table substitution

Date: 2026-07-30

## Technical summary

**This did not improve any of the 42 pages.** The fifth parser variant used the
same strict token bars at region scope, but no ruled table had both a safe one-to-one
geometry match and passing region tokens. `regions_table_cells` therefore equals
`regions` on 42 of 42 pages.

There were 12 eligible ruled table grids on
11 pages after the 3-row and 2-column furniture filters. Substitutions fired: 0. Preservation failures: 0.

The scope correction was sound, but region detection was the blocker. Partial-page
tables were usually only part of a larger prose/chart region, while dense full-page
tables were often split into several small regions. The next useful test is a table-box
region splitter, not a looser token threshold.

## The four partial-page tables did not reach substitution

These are listed first as requested. Recall is against navigation-stripped body words.
The before/after blocks show the exact compared text; they are unchanged when the
substitution did not fire.

### AEO page 5

- Substitution fired: **no**.
- Decision: no region reached the 80% containment bar.
- Whole-page recall: 0.2515 (25.1%); extra-token ratio: 0.0000.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: none.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
WATER GOALS
Current Goal: Reduce water use per jean by 50% by 2025
Previous Goal: 30% reduction (surpassed in 2021)
Includes fabric and garment production (mill & laundry)
WATER USE PER JEAN
150
125
100
75
50
2017 2018 2019 2020 2021 2022 2023 2024
Gallons Billion Gallons
Year Reduction
of Water of Water
2017 Baseline – –
2018 -10% 3 0.7
2019 -16% 5 1.2
2020 -23% 8 2.0
2021 -36% 12 3.5
2022 -38% 13 4.1
2023 -40% 14 4.4
2024 -48% 16 5.6

Current Goal: Recycle 70% of total water in denim laundries by 2025
Previous Goal: Recycle 50% by 2023 (surpassed in 2022)
Water recycling rate: recycled amount / fresh water amount
% Recycled Eligible Jean Million gallons
Year
In Production Laundries of water
2017 12% 24% 2
2018 14% 27% 2
2019 25% 68% 4
2020 27% 78% 4
2021 45% 100% 7
2022 64% 100% 9
2023 73% 100% 9
2024 76% 97% 10

*Supplier data currently under third party verification.
05
05
~~~

</details>

<details><summary>After text</summary>

~~~text
WATER GOALS
Current Goal: Reduce water use per jean by 50% by 2025
Previous Goal: 30% reduction (surpassed in 2021)
Includes fabric and garment production (mill & laundry)
WATER USE PER JEAN
150
125
100
75
50
2017 2018 2019 2020 2021 2022 2023 2024
Gallons Billion Gallons
Year Reduction
of Water of Water
2017 Baseline – –
2018 -10% 3 0.7
2019 -16% 5 1.2
2020 -23% 8 2.0
2021 -36% 12 3.5
2022 -38% 13 4.1
2023 -40% 14 4.4
2024 -48% 16 5.6

Current Goal: Recycle 70% of total water in denim laundries by 2025
Previous Goal: Recycle 50% by 2023 (surpassed in 2022)
Water recycling rate: recycled amount / fresh water amount
% Recycled Eligible Jean Million gallons
Year
In Production Laundries of water
2017 12% 24% 2
2018 14% 27% 2
2019 25% 68% 4
2020 27% 78% 4
2021 45% 100% 7
2022 64% 100% 9
2023 73% 100% 9
2024 76% 97% 10

*Supplier data currently under third party verification.
05
05
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| Year | % Recycled In Production | Eligible Jean Laundries | Million gallons of water |
| --- | --- | --- | --- |
| 2017 | 12% | 24% | 2 |
| 2018 | 14% | 27% | 2 |
| 2019 | 25% | 68% | 4 |
| 2020 | 27% | 78% | 4 |
| 2021 | 45% | 100% | 7 |
| 2022 | 64% | 100% | 9 |
| 2023 | 73% | 100% | 9 |
| 2024 | 76% | 97% | 10 |
~~~

</details>

### ACI page 14

- Substitution fired: **no**.
- Decision: no region reached the 80% containment bar.
- Whole-page recall: 0.3263 (32.6%); extra-token ratio: 0.0000.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: none.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
Representation
Representation at Albertsons Companies
Over the past few years, Albertsons Companies has

2021 2022
made strides in improving the representation of diverse
COMPANY US COMPANY US
associates in our management and across the company to VP & ABOVE VP & ABOVE
WIDE DEMOGRAPHICS WIDE DEMOGRAPHICS
reflect the communities we serve. In 2022, we expanded
GENDER
the ways that associates can self-identify within gender
MALE 67% 51% 49% 68% 50% 50%
and ethnicity designations.
FEMALE 33% 49% 51% 32% 49% 50%
Diversity Councils NON-BINARY 0% <1%
NON-DISCLOSED 0% <1%
Our National Diversity Council is chaired by our CEO
Vivek Sankaran and works to advance DE&I across ETHNICITY
Albertsons Companies. This group of executive sponsors WHITE 75% 54% 59% 71% 52% 59%
and leaders is focused on promoting and growing
PEOPLE OF COLOR 21% 39% 39% 29% 45% 44%
Diversity, Equity & Inclusion to help our company be the
BLACK/AFRICAN
best place to work and shop. We have Diversity Councils 5% 11% 14% 5% 10% 14%
AMERICAN
for our 12 operating divisions and individual councils for
HISPANIC/LATINO 4% 21% 19% 6% 21% 19%
our Technology & Engineering, Digital & Consumer, and
Supply Chain departments. ASIAN/ASIAN
12% 7% 6% 14% 6% 6%
AMERICAN
TWO OR
Social Justice Grant Program
MORE RACES 3% 7% 3%
(MULTI-ETHNIC)
In 2020, we launched our Social Justice Grant program to
NATIVE-HAWAIIAN/
support efforts to promote equality in the communities <1% <1% <1%
PACIFIC ISLANDER
we serve. Since its inception, our Social Justice
Grant program has donated over $3 million towards NATIVE AMERICAN
<1% 1% 1%
OR ALASKA NATIVE
organizations that share this mission. In 2022, we
donated to numerous organizations that strengthen civic
OTHER/NON-DISCLOSED 4% 7% 5% 0% 3% 0%
engagement, develop multicultural professionals, and
support efforts to promote diversity and equality. U.S. Demographics data from U.S. Census Bureau. Cells colored grey were not available before 2022. See 2021/2022 ESG Report for 2020 data.
2021 data does not include United. Ethnicity data is from U.S. operations only.

14 14
~~~

</details>

<details><summary>After text</summary>

~~~text
Representation
Representation at Albertsons Companies
Over the past few years, Albertsons Companies has

2021 2022
made strides in improving the representation of diverse
COMPANY US COMPANY US
associates in our management and across the company to VP & ABOVE VP & ABOVE
WIDE DEMOGRAPHICS WIDE DEMOGRAPHICS
reflect the communities we serve. In 2022, we expanded
GENDER
the ways that associates can self-identify within gender
MALE 67% 51% 49% 68% 50% 50%
and ethnicity designations.
FEMALE 33% 49% 51% 32% 49% 50%
Diversity Councils NON-BINARY 0% <1%
NON-DISCLOSED 0% <1%
Our National Diversity Council is chaired by our CEO
Vivek Sankaran and works to advance DE&I across ETHNICITY
Albertsons Companies. This group of executive sponsors WHITE 75% 54% 59% 71% 52% 59%
and leaders is focused on promoting and growing
PEOPLE OF COLOR 21% 39% 39% 29% 45% 44%
Diversity, Equity & Inclusion to help our company be the
BLACK/AFRICAN
best place to work and shop. We have Diversity Councils 5% 11% 14% 5% 10% 14%
AMERICAN
for our 12 operating divisions and individual councils for
HISPANIC/LATINO 4% 21% 19% 6% 21% 19%
our Technology & Engineering, Digital & Consumer, and
Supply Chain departments. ASIAN/ASIAN
12% 7% 6% 14% 6% 6%
AMERICAN
TWO OR
Social Justice Grant Program
MORE RACES 3% 7% 3%
(MULTI-ETHNIC)
In 2020, we launched our Social Justice Grant program to
NATIVE-HAWAIIAN/
support efforts to promote equality in the communities <1% <1% <1%
PACIFIC ISLANDER
we serve. Since its inception, our Social Justice
Grant program has donated over $3 million towards NATIVE AMERICAN
<1% 1% 1%
OR ALASKA NATIVE
organizations that share this mission. In 2022, we
donated to numerous organizations that strengthen civic
OTHER/NON-DISCLOSED 4% 7% 5% 0% 3% 0%
engagement, develop multicultural professionals, and
support efforts to promote diversity and equality. U.S. Demographics data from U.S. Census Bureau. Cells colored grey were not available before 2022. See 2021/2022 ESG Report for 2020 data.
2021 data does not include United. Ethnicity data is from U.S. operations only.

14 14
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| Representation at Albertsons Companies |  |  |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  | 2021 |  |  | 2022 |  |
|  | VP & ABOVE |  | COMPANY WIDE | US DEMOGRAPHICS | VP & ABOVE | COMPANY WIDE | US DEMOGRAPHICS |
| GENDER |  |  |  |  |  |  |  |
| MALE | 67% |  | 51% | 49% | 68% | 50% | 50% |
| FEMALE | 33% |  | 49% | 51% | 32% | 49% | 50% |
| NON-BINARY |  |  |  |  | 0% | <1% |  |
| NON-DISCLOSED |  |  |  |  | 0% | <1% |  |
| ETHNICITY |  |  |  |  |  |  |  |
| WHITE | 75% |  | 54% | 59% | 71% | 52% | 59% |
| PEOPLE OF COLOR | 21% |  | 39% | 39% | 29% | 45% | 44% |
| BLACK/AFRICAN AMERICAN | 5% |  | 11% | 14% | 5% | 10% | 14% |
| HISPANIC/LATINO | 4% |  | 21% | 19% | 6% | 21% | 19% |
| ASIAN/ASIAN AMERICAN | 12% |  | 7% | 6% | 14% | 6% | 6% |
| TWO OR MORE RACES (MULTI-ETHNIC) |  |  |  |  | 3% | 7% | 3% |
| NATIVE-HAWAIIAN/ PACIFIC ISLANDER |  |  |  |  | <1% | <1% | <1% |
| NATIVE AMERICAN OR ALASKA NATIVE |  |  |  |  | <1% | 1% | 1% |
| OTHER/NON-DISCLOSED | 4% |  | 7% | 5% | 0% | 3% | 0% |
~~~

</details>

### AEO page 10

- Substitution fired: **no**.
- Decision: no region reached the 80% containment bar.
- Whole-page recall: 0.4749 (47.5%); extra-token ratio: 0.0000.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: none.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
2024 SUSTAINABLE POLYESTER BREAKDOWN

2024 PROGRESS
2024 ACHIEVED:
63% of total polyester recycled
2028 GOAL:
100% sustainable polyester

RECYCLED POYLESTER BY BRAND
80%
75%
63% 63% 64%
61%
60% 57%
47%
40%

PLASTIC BOTTLES:
AEO used the equivalent of more
than 634 million plastic bottles in
recycled polyester.
American Eagle used the equivalent
of more than 425 million plastic
bottles in recycled polyester.
Aerie used the equivalent of
nearly 136 million plastic bottles
in recycled polyester.

20%
5%
0
AEO AE AE Men’s AE AE77 Offline Unsubscribed
Aerie
Women’s
Total Total
Brand Total Recycled (kg) Total Recycled (lbs) Water Bottles
AEO Total 10,658,238 23,497,392 634,429,572
AE Total 7,146,432 15,755,186 425,390,013
AE Men’s 2,370,804 5,226,728 141,121,660
AE Women’s 4,775,628 10,528,458 284,268,353
AE77 239 527 14,226
Aerie 2,277,640 5,021,337 135,576,090
OFFLINE 1,232,086 2,716,285 73,339,686
Unsubscribed 105 231 6,250

10
~~~

</details>

<details><summary>After text</summary>

~~~text
2024 SUSTAINABLE POLYESTER BREAKDOWN

2024 PROGRESS
2024 ACHIEVED:
63% of total polyester recycled
2028 GOAL:
100% sustainable polyester

RECYCLED POYLESTER BY BRAND
80%
75%
63% 63% 64%
61%
60% 57%
47%
40%

PLASTIC BOTTLES:
AEO used the equivalent of more
than 634 million plastic bottles in
recycled polyester.
American Eagle used the equivalent
of more than 425 million plastic
bottles in recycled polyester.
Aerie used the equivalent of
nearly 136 million plastic bottles
in recycled polyester.

20%
5%
0
AEO AE AE Men’s AE AE77 Offline Unsubscribed
Aerie
Women’s
Total Total
Brand Total Recycled (kg) Total Recycled (lbs) Water Bottles
AEO Total 10,658,238 23,497,392 634,429,572
AE Total 7,146,432 15,755,186 425,390,013
AE Men’s 2,370,804 5,226,728 141,121,660
AE Women’s 4,775,628 10,528,458 284,268,353
AE77 239 527 14,226
Aerie 2,277,640 5,021,337 135,576,090
OFFLINE 1,232,086 2,716,285 73,339,686
Unsubscribed 105 231 6,250

10
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| Brand | Total Recycled (kg) | Total Recycled (lbs) | Water Bottles |
| --- | --- | --- | --- |
| AEO Total | 10,658,238 | 23,497,392 | 634,429,572 |
| AE Total | 7,146,432 | 15,755,186 | 425,390,013 |
| AE Men’s | 2,370,804 | 5,226,728 | 141,121,660 |
| AE Women’s | 4,775,628 | 10,528,458 | 284,268,353 |
| AE77 | 239 | 527 | 14,226 |
| Aerie | 2,277,640 | 5,021,337 | 135,576,090 |
| OFFLINE | 1,232,086 | 2,716,285 | 73,339,686 |
| Unsubscribed | 105 | 231 | 6,250 |
~~~

</details>

### AEO page 6

- Substitution fired: **no**.
- Decision: no region reached the 80% containment bar.
- Whole-page recall: 0.4833 (48.3%); extra-token ratio: 0.0000.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: none.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
2024 REAL GOOD BY THE NUMBERS
% of 2020 2021 2022 2023 2024
Real Good
Styles YoY
100%
97% 98% 98%
95% 94%
92% 91%
89% 87%86% 88% 88% 87%
86%
83%
86% 81%
75% 80%
70% 72% 75% 71%70%
67% 69%
66% 65%
58% 55% 57%
50% 54%
48% 45% 44% 44% 47%
45% 45%
33%
25%
25% 31%
18% 18% 20% 19%
7%
0%
OFFLINE Intimates Aerie AE Women’s AE Women’s AE Women AE Men’s AE Men’s AE Men Total AEO
Tops Bottoms Tops Bottoms
BRAND / CATEGORY 2020 2021 2022 2023 2024
Total OFFLINE 45% 48%
Total Intimates 18% 25% 45% 67% 89%
Total Aerie 18% 34% 44% 70% 86%
Total AE Women’s Tops 7% 66% 72% 44% 54%
Total AE Women’s Bottoms 55% 81% 97% 87% 86%
Total AE Women 20% 58% 83% 57% 65%
Total AE Men’s Tops 31% 88% 98% 88% 80%
Total AE Men’s Bottoms 45% 95% 92% 98% 87%
Total AE Men 75% 94% 91% 86%
Total AEO 19% 47% 71% 70% 69%

06
~~~

</details>

<details><summary>After text</summary>

~~~text
2024 REAL GOOD BY THE NUMBERS
% of 2020 2021 2022 2023 2024
Real Good
Styles YoY
100%
97% 98% 98%
95% 94%
92% 91%
89% 87%86% 88% 88% 87%
86%
83%
86% 81%
75% 80%
70% 72% 75% 71%70%
67% 69%
66% 65%
58% 55% 57%
50% 54%
48% 45% 44% 44% 47%
45% 45%
33%
25%
25% 31%
18% 18% 20% 19%
7%
0%
OFFLINE Intimates Aerie AE Women’s AE Women’s AE Women AE Men’s AE Men’s AE Men Total AEO
Tops Bottoms Tops Bottoms
BRAND / CATEGORY 2020 2021 2022 2023 2024
Total OFFLINE 45% 48%
Total Intimates 18% 25% 45% 67% 89%
Total Aerie 18% 34% 44% 70% 86%
Total AE Women’s Tops 7% 66% 72% 44% 54%
Total AE Women’s Bottoms 55% 81% 97% 87% 86%
Total AE Women 20% 58% 83% 57% 65%
Total AE Men’s Tops 31% 88% 98% 88% 80%
Total AE Men’s Bottoms 45% 95% 92% 98% 87%
Total AE Men 75% 94% 91% 86%
Total AEO 19% 47% 71% 70% 69%

06
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| BRAND / CATEGORY | 2020 | 2021 | 2022 | 2023 | 2024 |
| --- | --- | --- | --- | --- | --- |
| Total OFFLINE |  |  |  | 45% | 48% |
| Total Intimates | 18% | 25% | 45% | 67% | 89% |
| Total Aerie | 18% | 34% | 44% | 70% | 86% |
| Total AE Women’s Tops | 7% | 66% | 72% | 44% | 54% |
| Total AE Women’s Bottoms | 55% | 81% | 97% | 87% | 86% |
| Total AE Women | 20% | 58% | 83% | 57% | 65% |
| Total AE Men’s Tops | 31% | 88% | 98% | 88% | 80% |
| Total AE Men’s Bottoms | 45% | 95% | 92% | 98% | 87% |
| Total AE Men |  | 75% | 94% | 91% | 86% |
| Total AEO | 19% | 47% | 71% | 70% | 69% |
~~~

</details>

## Near-misses and ACI page 30 also stayed unchanged

### ACI page 29

- Substitution fired: **no**.
- Decision: one table overlaps several regions; left unchanged.
- Whole-page recall: 0.9810 (98.1%); extra-token ratio: 0.0000.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
Zero food waste to landfill
WASTE REDUCTION
& CIRCULARITY
Increase the recyclability, reusability,
and/or compostability of our Own Brands
packaging by 2025
Enable the donation of
1 billion meals by 2030

--- region boundary ---

• Continued efforts to prevent food waste, donate edible food,
and divert inedible food from landfill. Expanded store food
donation programs to include new categories and had over
90% of stores donating weekly by the end of 2022.
• Diverted more than 321 million pounds of food and trimmings
through inedible food waste diversion programs that are in
place in the majority of our stores.
• Established our Own Brands primary plastics and packaging baseline.
• Achieved providing standardized recycling communications
on all Own Brands products by 2022.
• Enabled 254 million meals in 2022, and more than 950
million meals since 2019.

--- region boundary ---

COMMUNITY
STEWARDSHIP
Champion innovative programs
and partnerships to help break
the cycle of hunger

--- region boundary ---

• Continued to build partnerships and launch new
programs to help break the cycle of hunger.

--- region boundary ---

REPORT REFERENCE
Climate Action – Page 9
Value Chain Emissions – Page 10
Value Chain Emissions – Page 10
Representation – Page 14

--- region boundary ---

Inclusion – Page 15
Inclusion – Page 15
Training & Development – Page 16
Preventing Food Waste – Page 18
Donating Edible Food – Page 18
Diverting Inedible Food – Page 18
Plastics and Packaging – Page 19
Recycling Communications – Page 19

--- region boundary ---

Donating Food from our Stores – Page 23
Albertsons Companies Foundation
Nourishing Neighbors Program - Page 23

--- region boundary ---

Engaging Students in Finding Solutions
in Their Communities – Page 24
Teaming Up with State Governments to
Increase Access to Healthy Foods – Page 24
Improving Food Access – Page 24
Supporting the White House Conference
on Hunger, Nutrition, and Health – Page 24
~~~

</details>

<details><summary>After text</summary>

~~~text
Zero food waste to landfill
WASTE REDUCTION
& CIRCULARITY
Increase the recyclability, reusability,
and/or compostability of our Own Brands
packaging by 2025
Enable the donation of
1 billion meals by 2030

--- region boundary ---

• Continued efforts to prevent food waste, donate edible food,
and divert inedible food from landfill. Expanded store food
donation programs to include new categories and had over
90% of stores donating weekly by the end of 2022.
• Diverted more than 321 million pounds of food and trimmings
through inedible food waste diversion programs that are in
place in the majority of our stores.
• Established our Own Brands primary plastics and packaging baseline.
• Achieved providing standardized recycling communications
on all Own Brands products by 2022.
• Enabled 254 million meals in 2022, and more than 950
million meals since 2019.

--- region boundary ---

COMMUNITY
STEWARDSHIP
Champion innovative programs
and partnerships to help break
the cycle of hunger

--- region boundary ---

• Continued to build partnerships and launch new
programs to help break the cycle of hunger.

--- region boundary ---

REPORT REFERENCE
Climate Action – Page 9
Value Chain Emissions – Page 10
Value Chain Emissions – Page 10
Representation – Page 14

--- region boundary ---

Inclusion – Page 15
Inclusion – Page 15
Training & Development – Page 16
Preventing Food Waste – Page 18
Donating Edible Food – Page 18
Diverting Inedible Food – Page 18
Plastics and Packaging – Page 19
Recycling Communications – Page 19

--- region boundary ---

Donating Food from our Stores – Page 23
Albertsons Companies Foundation
Nourishing Neighbors Program - Page 23

--- region boundary ---

Engaging Students in Finding Solutions
in Their Communities – Page 24
Teaming Up with State Governments to
Increase Access to Healthy Foods – Page 24
Improving Food Access – Page 24
Supporting the White House Conference
on Hunger, Nutrition, and Health – Page 24
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| TOPIC | GOAL | 2022 PROGRESS | REPORT REFERENCE |
| --- | --- | --- | --- |
| CLIMATE ACTION | Reduce scope 1 and 2 emissions by 47% by 2030 from a 2019 baseline | • 21% reduction from a 2019 baseline. | Climate Action – Page 9 |
|  | Engage top suppliers to set science-based carbon reduction goals based on a 2019 baseline | • More than 80 suppliers have set, committed to setting, and/or tracked their progress against science-based reduction targets. | Value Chain Emissions – Page 10 |
|  | Reduce emissions from the use of sold goods by 27.5% from a 2019 baseline | • More than a 5% reduction from a 2019 baseline. | Value Chain Emissions – Page 10 |
| DIVERSITY, EQUITY & INCLUSION | Increase diverse representation within our management | • In 2022, we expanded the ways that associates can self-identify within gender and ethnicity designations. | Representation – Page 14 |
|  | Foster an inclusive culture that embraces differences | • Established an inclusion index to foster a culture that puts people first and values diverse perspectives. | Inclusion – Page 15 |
|  | Ensure all associates have equal access to opportunities and resources | • Continued to expand programs and offer training and development opportunities. | Inclusion – Page 15 Training & Development – Page 16 |
| WASTE REDUCTION & CIRCULARITY | Zero food waste to landfill | • Continued efforts to prevent food waste, donate edible food, and divert inedible food from landfill. Expanded store food donation programs to include new categories and had over 90% of stores donating weekly by the end of 2022. • Diverted more than 321 million pounds of food and trimmings through inedible food waste diversion programs that are in place in the majority of our stores. | Preventing Food Waste – Page 18 Donating Edible Food – Page 18 Diverting Inedible Food – Page 18 |
|  | Increase the recyclability, reusability, and/or compostability of our Own Brands packaging by 2025 | • Established our Own Brands primary plastics and packaging baseline. • Achieved providing standardized recycling communications on all Own Brands products by 2022. | Plastics and Packaging – Page 19 Recycling Communications – Page 19 |
| COMMUNITY STEWARDSHIP | Enable the donation of 1 billion meals by 2030 | • Enabled 254 million meals in 2022, and more than 950 million meals since 2019. | Donating Food from our Stores – Page 23 Albertsons Companies Foundation Nourishing Neighbors Program - Page 23 |
|  | Champion innovative programs and partnerships to help break the cycle of hunger | • Continued to build partnerships and launch new programs to help break the cycle of hunger. | Engaging Students in Finding Solutions in Their Communities – Page 24 Teaming Up with State Governments to Increase Access to Healthy Foods – Page 24 Improving Food Access – Page 24 Supporting the White House Conference on Hunger, Nutrition, and Health – Page 24 |
~~~

</details>

### AEO page 4

- Substitution fired: **no**.
- Decision: region recall 0.9797 is below 0.9950.
- Whole-page recall: 0.9757 (97.6%); extra-token ratio: 0.0000.
- Region recall: 0.9797; region extra-token ratio: 0.0000.
- Matched region types: row_structured.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
BUILDING A BETTER PLANET - GOALS
FOCUS AREA GOAL ESTABLISHED STATUS PROGRESS + ACHIEVEMENTS
Consumer take back programs include Blue
Collect post-consumer apparel, diverting waste from
2019 ON TRACK Jeans Go Green (American Eagle) and I Support
landfills with a goal to increase volume every year
the Girls (Aerie)
All hangtags and product labels are sustainably
Convert all labels to sustainably sourced materials 2019
ACHIEVED sourced and will continue to be
Recycle 100% of pre-consumer apparel waste at factories
2022 ON TRACK Waste assessment at factories underway
by 2028
WASTE
REDUCTION
Keep unsellable garments (returns and QA issues, product Partnered with a network of nonprofits to reuse
2022 ON TRACK
safety issues) from landfills by 2028 +recycle unsellable garments
Reduce virgin plastic by 50% and reduce total
2022 ON TRACK Initial work underway
plastic footprint by 30% by 2028
Use sustainable sources for 75% of all fibers by 2028 2022 ON TRACK 62% of all fibers are sustainably sourced
•100% of cotton fiber 2019 ON TRACK 67% of cotton was sustainably sourced
•100% of man-made cellulose fibers 2019 ON TRACK 92% of cellulosics were sustainably sourced
•20% of all-natural fiber volume will come from 6% of all-natural fiber volume will come from
SUSTAINABLE 2022 ON TRACK
recycled materials recycled materials
MATERIALS
•50% of nylon fiber 2022 ON TRACK 44% of nylon was sustainably sourced
Goal was set at 50% in 2019 and updated in
•100% of polyester fiber 2022 ON TRACK
2022; 63% of polyester was sustainably sourced
~~~

</details>

<details><summary>After text</summary>

~~~text
BUILDING A BETTER PLANET - GOALS
FOCUS AREA GOAL ESTABLISHED STATUS PROGRESS + ACHIEVEMENTS
Consumer take back programs include Blue
Collect post-consumer apparel, diverting waste from
2019 ON TRACK Jeans Go Green (American Eagle) and I Support
landfills with a goal to increase volume every year
the Girls (Aerie)
All hangtags and product labels are sustainably
Convert all labels to sustainably sourced materials 2019
ACHIEVED sourced and will continue to be
Recycle 100% of pre-consumer apparel waste at factories
2022 ON TRACK Waste assessment at factories underway
by 2028
WASTE
REDUCTION
Keep unsellable garments (returns and QA issues, product Partnered with a network of nonprofits to reuse
2022 ON TRACK
safety issues) from landfills by 2028 +recycle unsellable garments
Reduce virgin plastic by 50% and reduce total
2022 ON TRACK Initial work underway
plastic footprint by 30% by 2028
Use sustainable sources for 75% of all fibers by 2028 2022 ON TRACK 62% of all fibers are sustainably sourced
•100% of cotton fiber 2019 ON TRACK 67% of cotton was sustainably sourced
•100% of man-made cellulose fibers 2019 ON TRACK 92% of cellulosics were sustainably sourced
•20% of all-natural fiber volume will come from 6% of all-natural fiber volume will come from
SUSTAINABLE 2022 ON TRACK
recycled materials recycled materials
MATERIALS
•50% of nylon fiber 2022 ON TRACK 44% of nylon was sustainably sourced
Goal was set at 50% in 2019 and updated in
•100% of polyester fiber 2022 ON TRACK
2022; 63% of polyester was sustainably sourced
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| FOCUS AREA | GOAL | ESTABLISHED | STATUS | PROGRESS + ACHIEVEMENTS |
| --- | --- | --- | --- | --- |
| WASTE REDUCTION | Collect post-consumer apparel, diverting waste from landfills with a goal to increase volume every year | 2019 | ON TRACK | Consumer take back programs include Blue Jeans Go Green (American Eagle) and I Support the Girls (Aerie) |
|  | Convert all labels to sustainably sourced materials | 2019 | ACHIEVED | All hangtags and product labels are sustainably sourced and will continue to be |
|  | Recycle 100% of pre-consumer apparel waste at factories by 2028 | 2022 | ON TRACK | Waste assessment at factories underway |
|  | Keep unsellable garments (returns and QA issues, product safety issues) from landfills by 2028 | 2022 | ON TRACK | Partnered with a network of nonprofits to reuse +recycle unsellable garments |
|  | Reduce virgin plastic by 50% and reduce total plastic footprint by 30% by 2028 | 2022 | ON TRACK | Initial work underway |
| SUSTAINABLE MATERIALS | Use sustainable sources for 75% of all fibers by 2028 | 2022 | ON TRACK | 62% of all fibers are sustainably sourced |
|  | •100% of cotton fiber | 2019 | ON TRACK | 67% of cotton was sustainably sourced |
|  | •100% of man-made cellulose fibers | 2019 | ON TRACK | 92% of cellulosics were sustainably sourced |
|  | •20% of all-natural fiber volume will come from recycled materials | 2022 | ON TRACK | 6% of all-natural fiber volume will come from recycled materials |
|  | •50% of nylon fiber | 2022 | ON TRACK | 44% of nylon was sustainably sourced |
|  | •100% of polyester fiber | 2022 | ON TRACK | Goal was set at 50% in 2019 and updated in 2022; 63% of polyester was sustainably sourced |
~~~

</details>

### AEO page 3

- Substitution fired: **no**.
- Decision: one table overlaps several regions; left unchanged.
- Whole-page recall: 0.9498 (95.0%); extra-token ratio: 0.0000.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: row_structured, single_column_prose, single_column_prose.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
BUILDING A BETTER PLANET - GOALS
FOCUS AREA GOAL ESTABLISHED STATUS PROGRESS + ACHIEVEMENTS
Reduce water use per jean by 30% by 2023 from a 2017 Reduced water usage per jean by 36% in 2021,
2019
baseline year EXCEEDED meeting goal two years early
Reduce water use per jean by 50% by 2025 from a Reduced water usage by 48% in 2024. Target
2022 ON TRACK
2017 baseline year increased, after meeting our initial goal
Reached an overall recycling rate of 64% in 2022,
Recycle 50% of total water used in denim laundries by 2023 2019
EXCEEDED exceeding goal one year early
Recycle 70% of total water used in denim laundries by 2025 Reached an overall recycling rate of 76%,
2022
WATER EXCEEDED meeting goal two years early
Apply AEO Wastewater Management Standard to 100%
2019 As of 2021, 100% of strategic water-intensive
of strategic water-intensive factories, mills and laundries
ACHIEVED factories conduct wastewater testing annually

--- region boundary ---

by 2023
Reduce water footprint by 30% by 2028 across own
operations and strategic factories and mills for all
product types
AEO commits to securing renewable energy for 100% of
electrical power demand for owned and operated facilities
by 2030
Reduce scope 1 & 2 emissions 80% by 2030 from a
2018 base year
Reduce carbon emissions 40% by 2030 and 60% by 2040
CLIMATE
in AEO’s manufacturing from a 2018 base year
Phase out coal-fired boilers in our supply chain by 2030;
no new factories with coal-fired boilers after 2025

--- region boundary ---

Reduction work launched, discussions underway
2022
ON TRACK
with suppliers
2019 ON TRACK AEO reached 24% renewable energy in 2024
Emissions decreased 56% from our baseline
2019 ON TRACK
in 2024
Total emissions from tier 1 factories reduced
2019 ON TRACK 11% from 2023 to 2024; procurement volume
increased 1%.
16 suppliers phased out coal in 2024, more
2022 ON TRACK
are underway
~~~

</details>

<details><summary>After text</summary>

~~~text
BUILDING A BETTER PLANET - GOALS
FOCUS AREA GOAL ESTABLISHED STATUS PROGRESS + ACHIEVEMENTS
Reduce water use per jean by 30% by 2023 from a 2017 Reduced water usage per jean by 36% in 2021,
2019
baseline year EXCEEDED meeting goal two years early
Reduce water use per jean by 50% by 2025 from a Reduced water usage by 48% in 2024. Target
2022 ON TRACK
2017 baseline year increased, after meeting our initial goal
Reached an overall recycling rate of 64% in 2022,
Recycle 50% of total water used in denim laundries by 2023 2019
EXCEEDED exceeding goal one year early
Recycle 70% of total water used in denim laundries by 2025 Reached an overall recycling rate of 76%,
2022
WATER EXCEEDED meeting goal two years early
Apply AEO Wastewater Management Standard to 100%
2019 As of 2021, 100% of strategic water-intensive
of strategic water-intensive factories, mills and laundries
ACHIEVED factories conduct wastewater testing annually

--- region boundary ---

by 2023
Reduce water footprint by 30% by 2028 across own
operations and strategic factories and mills for all
product types
AEO commits to securing renewable energy for 100% of
electrical power demand for owned and operated facilities
by 2030
Reduce scope 1 & 2 emissions 80% by 2030 from a
2018 base year
Reduce carbon emissions 40% by 2030 and 60% by 2040
CLIMATE
in AEO’s manufacturing from a 2018 base year
Phase out coal-fired boilers in our supply chain by 2030;
no new factories with coal-fired boilers after 2025

--- region boundary ---

Reduction work launched, discussions underway
2022
ON TRACK
with suppliers
2019 ON TRACK AEO reached 24% renewable energy in 2024
Emissions decreased 56% from our baseline
2019 ON TRACK
in 2024
Total emissions from tier 1 factories reduced
2019 ON TRACK 11% from 2023 to 2024; procurement volume
increased 1%.
16 suppliers phased out coal in 2024, more
2022 ON TRACK
are underway
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| FOCUS AREA | GOAL | ESTABLISHED | STATUS | PROGRESS + ACHIEVEMENTS |
| --- | --- | --- | --- | --- |
| WATER | Reduce water use per jean by 30% by 2023 from a 2017 baseline year | 2019 | EXCEEDED | Reduced water usage per jean by 36% in 2021, meeting goal two years early |
|  | Reduce water use per jean by 50% by 2025 from a 2017 baseline year | 2022 | ON TRACK | Reduced water usage by 48% in 2024. Target increased, after meeting our initial goal |
|  | Recycle 50% of total water used in denim laundries by 2023 | 2019 | EXCEEDED | Reached an overall recycling rate of 64% in 2022, exceeding goal one year early |
|  | Recycle 70% of total water used in denim laundries by 2025 | 2022 | EXCEEDED | Reached an overall recycling rate of 76%, meeting goal two years early |
|  | Apply AEO Wastewater Management Standard to 100% of strategic water-intensive factories, mills and laundries by 2023 | 2019 | ACHIEVED | As of 2021, 100% of strategic water-intensive factories conduct wastewater testing annually |
|  | Reduce water footprint by 30% by 2028 across own operations and strategic factories and mills for all product types | 2022 | ON TRACK | Reduction work launched, discussions underway with suppliers |
| CLIMATE | AEO commits to securing renewable energy for 100% of electrical power demand for owned and operated facilities by 2030 | 2019 | ON TRACK | AEO reached 24% renewable energy in 2024 |
|  | Reduce scope 1 & 2 emissions 80% by 2030 from a 2018 base year | 2019 | ON TRACK | Emissions decreased 56% from our baseline in 2024 |
|  | Reduce carbon emissions 40% by 2030 and 60% by 2040 in AEO’s manufacturing from a 2018 base year | 2019 | ON TRACK | Total emissions from tier 1 factories reduced 11% from 2023 to 2024; procurement volume increased 1%. |
|  | Phase out coal-fired boilers in our supply chain by 2030; no new factories with coal-fired boilers after 2025 | 2022 | ON TRACK | 16 suppliers phased out coal in 2024, more are underway |
~~~

</details>

### ACI page 32

- Substitution fired: **no**.
- Decision: one table overlaps several regions; left unchanged.
- Whole-page recall: 0.9470 (94.7%); extra-token ratio: 0.0066.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: single_column_prose, single_column_prose.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
Appendix 4: SASB Table
ACCOUNTING 2022
TOPIC
METRIC DATA
FLEET FUEL MANAGEMENT Fleet fuel consumed, percentage renewable 3,084,296 | 32.17%
Gross global Scope 1 emissions
AIR EMISSIONS FROM REFRIGERATION 1,799,033
from refrigerants
Percentage of refrigerants consumed
AIR EMISSIONS FROM REFRIGERATION 94.96%
with zero ozone-depleting potential
Discussion of strategy to manage
MANAGEMENT OF ENVIRONMENTAL &
environmental and social risks within the See Animal Welfare section
SOCIAL IMPACTS IN THE SUPPLY CHAIN supply chain, including animal welfare
MANAGEMENT OF ENVIRONMENTAL & Discussion of strategies to reduce the
See Plastics & Packaging section
SOCIAL IMPACTS IN THE SUPPLY CHAIN environmental impact of packaging
Number of (1) retail locations and
ACTIVITY METRICS (1) 2,271 (2) 22
(2) distribution centers

--- region boundary ---

UNIT OF
CODE
MEASURE
Gigajoules (GJ), Percentage (%) FB-FR-110A.1
Metric tons (t) CO e FB-FR-110B.1
2
Percentage (%) by weight FB-FR-110B.2
N/A FB-FR-430A.3
N/A FB-FR-430A.4
Number FB-FR-000.A
~~~

</details>

<details><summary>After text</summary>

~~~text
Appendix 4: SASB Table
ACCOUNTING 2022
TOPIC
METRIC DATA
FLEET FUEL MANAGEMENT Fleet fuel consumed, percentage renewable 3,084,296 | 32.17%
Gross global Scope 1 emissions
AIR EMISSIONS FROM REFRIGERATION 1,799,033
from refrigerants
Percentage of refrigerants consumed
AIR EMISSIONS FROM REFRIGERATION 94.96%
with zero ozone-depleting potential
Discussion of strategy to manage
MANAGEMENT OF ENVIRONMENTAL &
environmental and social risks within the See Animal Welfare section
SOCIAL IMPACTS IN THE SUPPLY CHAIN supply chain, including animal welfare
MANAGEMENT OF ENVIRONMENTAL & Discussion of strategies to reduce the
See Plastics & Packaging section
SOCIAL IMPACTS IN THE SUPPLY CHAIN environmental impact of packaging
Number of (1) retail locations and
ACTIVITY METRICS (1) 2,271 (2) 22
(2) distribution centers

--- region boundary ---

UNIT OF
CODE
MEASURE
Gigajoules (GJ), Percentage (%) FB-FR-110A.1
Metric tons (t) CO e FB-FR-110B.1
2
Percentage (%) by weight FB-FR-110B.2
N/A FB-FR-430A.3
N/A FB-FR-430A.4
Number FB-FR-000.A
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| TOPIC | ACCOUNTING METRIC | 2022 DATA | UNIT OF MEASURE | CODE |
| --- | --- | --- | --- | --- |
| FLEET FUEL MANAGEMENT | Fleet fuel consumed, percentage renewable | 3,084,296 \| 32.17% | Gigajoules (GJ), Percentage (%) | FB-FR-110A.1 |
| AIR EMISSIONS FROM REFRIGERATION | Gross global Scope 1 emissions from refrigerants | 1,799,033 | Metric tons (t) COe 2 | FB-FR-110B.1 |
| AIR EMISSIONS FROM REFRIGERATION | Percentage of refrigerants consumed with zero ozone-depleting potential | 94.96% | Percentage (%) by weight | FB-FR-110B.2 |
| MANAGEMENT OF ENVIRONMENTAL & SOCIAL IMPACTS IN THE SUPPLY CHAIN | Discussion of strategy to manage environmental and social risks within the supply chain, including animal welfare | See Animal Welfare section | N/A | FB-FR-430A.3 |
| MANAGEMENT OF ENVIRONMENTAL & SOCIAL IMPACTS IN THE SUPPLY CHAIN | Discussion of strategies to reduce the environmental impact of packaging | See Plastics & Packaging section | N/A | FB-FR-430A.4 |
| ACTIVITY METRICS | Number of (1) retail locations and (2) distribution centers | (1) 2,271 (2) 22 | Number | FB-FR-000.A |
~~~

</details>

### ACI page 30

- Substitution fired: **no**.
- Decision: one table overlaps several regions; left unchanged.
- Whole-page recall: 0.8383 (83.8%); extra-token ratio: 0.0778.
- Region recall: not computed because the geometry match was not one-to-one.
- Matched region types: single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose, single_column_prose, heading, heading, heading, single_column_prose, single_column_prose.
- Before/after result: unchanged because no safe substitution passed.

<details><summary>Before text</summary>

~~~text
SUSTAINABLE
EFFORTS BY ALBERTSONS COMPANIES
DEVELOPMENT GOAL
• Goal to enable the donation of 1 billion meals by 2030
• Donating food from stores to local
2. ZERO HUNGER hunger relief organizations
• Fundraising for local hunger relief
organizations in our stores

--- region boundary ---

• Goal to reflect the communities we serve
and increase diverse representation at
5. GENDER EQUALITY our management level
• Supporting women-owned businesses through
our Supplier Diversity Program

--- region boundary ---

• Increasing generation and procurement
7. AFFORDABLE & of renewable energy
CLEAN ENERGY • Continuing to implement projects
to improve our energy efficiency
8. DECENT WORK & • Providing training and opportunities
ECONOMIC GROWTH for growth
11. SUSTAINABLE CITIES

--- region boundary ---

• Providing funds for disaster relief
& COMMUNITIES
• Committed to zero food waste to landfill by 2030
• Continuing work toward our Plastics and
12. RESPONSIBLE Packaging Pledge, and established our Own Brands
CONSUMPTION & primary plastics and packaging baseline
PRODUCTION
• Provide sustainable products and ingredients offerings,
including local offerings, USDA BioPreferred ingredients,
Fair Trade-certified coffee, etc.
• Science Based Targets initiative
(SBTi) approved carbon reduction
13. CLIMATE ACTION goal aligned with a 1.5°C pathway
• 2040 net zero goal in our own operations

--- region boundary ---

• Implementing our Responsible Seafood
14. LIFE BELOW WATER
Policy and Commitment

--- region boundary ---

REFERENCES IN REPORT
• Recipe for Change - Page 7
• Recipe for Change - Page 7
• Product: Food Waste - Page 18
• Product: Food Waste - Page 18
• • Community Community - - Page Page 23 23

--- region boundary ---

• Recipe for Change - Page 7
• People: Diversity, Equity & Inclusion - Page 13
• People: Diversity, Equity
& Inclusion - Page 13
• People: Supplier Diversity - Page 16
• People: Supplier Diversity - Page 16

--- region boundary ---

• Planet: Climate Action - Page 9

--- region boundary ---

• • People: People: Training Training and and Development Development - - Page Page 16 16

--- region boundary ---

• Community - Page 23

--- region boundary ---

• • Recipe Recipe for for Change Change - - Page Page 7 7
• • Product: Product: Waste Waste Reduction Reduction
& & Circularity Circularity - - Page Page 18 18
• Product: Sustainable Products
• & Product: Ingredients Sustainable - Page 20 Products
& Ingredients - Page 20

--- region boundary ---

• Recipe for Change - Page 7
• Recipe for Change - Page 7
• Planet: Climate Action - Page 9
• Planet: Climate Action - Page 9
• Product: Sustainable Products
& Ingredients - Page 20
~~~

</details>

<details><summary>After text</summary>

~~~text
SUSTAINABLE
EFFORTS BY ALBERTSONS COMPANIES
DEVELOPMENT GOAL
• Goal to enable the donation of 1 billion meals by 2030
• Donating food from stores to local
2. ZERO HUNGER hunger relief organizations
• Fundraising for local hunger relief
organizations in our stores

--- region boundary ---

• Goal to reflect the communities we serve
and increase diverse representation at
5. GENDER EQUALITY our management level
• Supporting women-owned businesses through
our Supplier Diversity Program

--- region boundary ---

• Increasing generation and procurement
7. AFFORDABLE & of renewable energy
CLEAN ENERGY • Continuing to implement projects
to improve our energy efficiency
8. DECENT WORK & • Providing training and opportunities
ECONOMIC GROWTH for growth
11. SUSTAINABLE CITIES

--- region boundary ---

• Providing funds for disaster relief
& COMMUNITIES
• Committed to zero food waste to landfill by 2030
• Continuing work toward our Plastics and
12. RESPONSIBLE Packaging Pledge, and established our Own Brands
CONSUMPTION & primary plastics and packaging baseline
PRODUCTION
• Provide sustainable products and ingredients offerings,
including local offerings, USDA BioPreferred ingredients,
Fair Trade-certified coffee, etc.
• Science Based Targets initiative
(SBTi) approved carbon reduction
13. CLIMATE ACTION goal aligned with a 1.5°C pathway
• 2040 net zero goal in our own operations

--- region boundary ---

• Implementing our Responsible Seafood
14. LIFE BELOW WATER
Policy and Commitment

--- region boundary ---

REFERENCES IN REPORT
• Recipe for Change - Page 7
• Recipe for Change - Page 7
• Product: Food Waste - Page 18
• Product: Food Waste - Page 18
• • Community Community - - Page Page 23 23

--- region boundary ---

• Recipe for Change - Page 7
• People: Diversity, Equity & Inclusion - Page 13
• People: Diversity, Equity
& Inclusion - Page 13
• People: Supplier Diversity - Page 16
• People: Supplier Diversity - Page 16

--- region boundary ---

• Planet: Climate Action - Page 9

--- region boundary ---

• • People: People: Training Training and and Development Development - - Page Page 16 16

--- region boundary ---

• Community - Page 23

--- region boundary ---

• • Recipe Recipe for for Change Change - - Page Page 7 7
• • Product: Product: Waste Waste Reduction Reduction
& & Circularity Circularity - - Page Page 18 18
• Product: Sustainable Products
• & Product: Ingredients Sustainable - Page 20 Products
& Ingredients - Page 20

--- region boundary ---

• Recipe for Change - Page 7
• Recipe for Change - Page 7
• Planet: Climate Action - Page 9
• Planet: Climate Action - Page 9
• Product: Sustainable Products
& Ingredients - Page 20
~~~

</details>

<details><summary>Ruled-table markdown considered</summary>

~~~markdown
| SUSTAINABLE DEVELOPMENT GOAL | EFFORTS BY ALBERTSONS COMPANIES | REFERENCES IN REPORT |  |  |
| --- | --- | --- | --- | --- |
| 2. ZERO HUNGER | • Goal to enable the donation of 1 billion meals by 2030 • Donating food from stores to local hunger relief organizations • Fundraising for local hunger relief organizations in our stores | • Recipe for Change - Page 7 • Recipe for Change - Page 7 • Product: Food Waste - Page 18 • Product: Food Waste - Page 18 •• CCoommmmuunniittyy -- PPaaggee 2233 |  |  |
| 5. GENDER EQUALITY | • Goal to reflect the communities we serve and increase diverse representation at our management level • Supporting women-owned businesses through our Supplier Diversity Program | • Recipe for Change - Page 7 • People: Diversity, Equity & Inclusion - Page 13 • People: Diversity, Equity & Inclusion - Page 13 • People: Supplier Diversity - Page 16 • People: Supplier Diversity - Page 16 |  |  |
| 7. AFFORDABLE & CLEAN ENERGY | • Increasing generation and procurement of renewable energy • Continuing to implement projects to improve our energy efficiency | • Planet: Climate Action - Page 9 |  |  |
| 8. DECENT WORK & ECONOMIC GROWTH | • Providing training and opportunities for growth | •• PPeeooppllee:: TTrraaiinniinngg aanndd DDeevveellooppmmeenntt -- PPaaggee 1166 |  |  |
| 11. SUSTAINABLE CITIES & COMMUNITIES | • Providing funds for disaster relief | • Community - Page 23 |  |  |
| 12. RESPONSIBLE CONSUMPTION & PRODUCTION | • Committed to zero food waste to landfill by 2030 • Continuing work toward our Plastics and Packaging Pledge, and established our Own Brands primary plastics and packaging baseline • Provide sustainable products and ingredients offerings, including local offerings, USDA BioPreferred ingredients, Fair Trade-certified coffee, etc. | •• RReecciippee ffoorr CChhaannggee -- PPaaggee 77 •• PPrroodduucctt:: WWaassttee RReedduuccttiioonn && CCiirrccuullaarriittyy -- PPaaggee 1188 • Product: Sustainable Products • &Pr Iondgurcetd: iSeunststa -i nPaabglee 2P0roducts & Ingredients - Page 20 |  |  |
| 13. CLIMATE ACTION | • Science Based Targets initiative (SBTi) approved carbon reduction goal aligned with a 1.5°C pathway • 2040 net zero goal in our own operations | • Recipe for Change - Page 7 • Recipe for Change - Page 7 • Planet: Climate Action - Page 9 • Planet: Climate Action - Page 9 |  |  |
| 14. LIFE BELOW WATER | • Implementing our Responsible Seafood Policy and Commitment | • Product: Sustainable Products & Ingredients - Page 20 |  |  |
~~~

</details>

## Match audit

The bbox rule counts a match when the table intersection covers at least 80% of
the region area. Exact bbox equality is not used because ruling-line boxes are
usually larger than navigation-stripped word boxes.

- Eligible ruled tables: 12.
- Tables with no matching region: 7.
- One table overlapping several regions: 4.
- Regions overlapped by several tables: 0.
- Table-region overlap pairs where the region was not `row_structured`: 24.
- Unique geometry matches that failed strict token verification: 1.
- Successful substitutions: 0.

The 24 non-row overlap pairs came from four tables whose ruling lines saw a grid
that the region classifier split into `single_column_prose` or `heading` pieces.
That is a classifier gap, not evidence that the token bars are too strict.

## Unruled-table review

Unruled data-table pages: **0**.

All 42 pages were reviewed as renders. The four pages with no ruled detection were AEO p2 and ACI p2, p3, and p28. None contains an unruled data table; ACI p2 is an unruled table of contents and was excluded as navigation.

## Scope and method

- Scope: all 10 AEO-2024 pages and all 32 ACI-2022 pages.
- Detection: `pdfplumber.find_tables` with vertical and horizontal `lines` strategies.
- Furniture filters: at least 3 non-empty rows and at least 2 columns.
- Verification: existing markdown shape, 0.995 token recall, and 0.005 maximum
  extra-token ratio, measured against one matched region's words.
- Substitution: unique matches only. All other regions stay in their existing order.
- Visual check: 60 dpi renders of all 42 pages for unruled data tables.

## Limits and robustness checks

- Region words and raw table cells are not the same source. Raw cell extraction
  produced extra garbled tokens on ACI p30 (0.0778) and ACI p32 (0.0066). These
  exceed the existing bar and were not subtracted or hidden.
- AEO p4 was the only unique geometry match. Its region recall was 0.9797, below
  0.995, because the region still includes text outside the ruled table.
- Multi-region tables were left unchanged. Replacing one piece or merging pieces
  without an explicit table-box split could lose or duplicate words.
- The fresh before/after Bundle 2 pilot JSON files are byte-identical.

## Recommended next step

Add a geometry stage that uses a verified ruled table bbox as a region boundary,
then rerun this same strict audit. Do not lower the token bars. The current result
shows that table-to-region alignment, not page-versus-region token scope, is the
main blocker on this sample.

## Further question

Can a table-box splitter preserve nearby chart and prose order on AEO p5, p6, p10,
and ACI p14 without creating page-specific rules? That is the smallest follow-up
that could turn the four partial-page negative results into a useful path.

Source artifacts: `parser_comparison.json`, `parser_comparison.html`, and the two
source PDFs. The visual sweep method and exclusions are recorded above.
