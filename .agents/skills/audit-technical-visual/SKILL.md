---
name: audit-technical-visual
description: Audit rendered engineering figures and equations against original sources in a fresh context.
---
# audit-technical-visual

Use this draft procedure to evaluate the lecture exchange contract. The registry controls production
activation. The procedure applies only after VEP freezes the source and response contract.

## Inputs and outputs

Read the frozen visual audit request and rendered HTML/PDF, figure data and available sources.
Use a fresh context with no author history. Check each diagram, equation and worked step against its
own evidence. Inspect coordinates, arrow directions, reference axes, units, domains, boundary conditions,
supports, loads, plotted values and captions. Inspect legibility, clipping and source labels in the PDF and
HTML. Record exact target IDs and source IDs with severity. Uncertain evidence or unavailable output remains
a finding or an explicit gap, depending on material impact.

## Validation and stop conditions

Recalculate important points independently and mark unsupported material assertions P1 or above.
Stop if request identity, source access, or the required independent context cannot be verified.

## Boundaries

Do not edit authoring files, carry over author self-assessments or issue a human approval.
Use the public CLI for deterministic validation and writes.

## Reading order

Inspect a complete PDF page and a narrow-screen section containing a derivation. Check that the operation and its rationale lead the eye, that related symbolic equalities stay together, and that provenance remains reachable without a row of evidence links after every paragraph. Verify source-note destinations and supplement labels. Report repeated answer-form labels on pure algebra or citations that interrupt the explanation as reader-facing defects.

Independently start at the overview, jump to a specific operation, and use the overview return link. Compare inline and displayed math glyphs, including vector weight and indices, at 320px and in the PDF. Inspect factual emphasis in the body separately from headings. A chapter list alone does not explain how its topics connect.

Inspect whether parallel items are scannable as a list and dependent actions have ordered steps. To check explanatory sufficiency, pick a key equation, identify when it applies, explain why the critical operation works, and reproduce the action from its diagram and equation. Follow each newly named symbol to a visible use in a nearby formula or figure, including what a geometric angle is measured from. Check the viewing side, handedness, in-plane arrows and out-of-plane dots/crosses with their first legend. Flag a missing geometric reference or a long label that duplicates the legend. Read the action attached to a topic against its evidence; distinguish recall, reasoning, exercise and actual observation. A keyword, repetition or priority alone does not establish the action.

Read each section with background disclosures closed. Check the objective and first-use definitions against the diagram and first formula. Open each question by keyboard and check its reasoning, typeset notation, narrow-screen layout and presence in the printed PDF.

Start one pass from the verified lecture sources, before enumerating the delivered figures. Identify the
apparatus, boundary, observation or calculated point the learner was asked to locate. Find its visual
counterpart in the actual lesson. A missing figure can be reported against the containing block or the
visuals coverage topic. Verify the first occurrence of a symbol in tables and captions as well as equations;
a later glossary cannot explain an earlier axis during a first reading.

Read each procedure with its explanations skipped. Check whether the problem, equation and instructions are sufficient to reproduce the intermediate answer. Follow every step-to-explanation link and return to its exact step. For drawing problems compare adjacent stages on the same axes, check that each operation has a distinct in-place figure with its new marks, and compare its coordinates and the scope of any cancellation mark against the equation. A single final plot cannot demonstrate an intermediate operation.
