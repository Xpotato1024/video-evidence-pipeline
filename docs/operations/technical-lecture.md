# Technical lecture review packages

VEP builds a private, versioned review candidate from an existing evidence workspace, an approved legacy
archive, or local registered documents. The lecture exchange has its own immutable stages. The source job's
transition history and its v1 approvals retain their original meaning.

## Run a lecture exchange

Install VEP and the renderer from the source checkout. Use the pinned dependencies and local browser
installed by `npm ci --prefix renderer` and `npx --prefix renderer playwright install chromium webkit`.
Install Poppler's `pdftotext` and `pdftoppm` for direct PDF sources. Install a Japanese font with the glyphs
required by the lecture when producing Japanese PDF output.

```sh
vep lecture prepare WORKSPACE --request options.json --destination prepared --json
vep lecture request-validate prepared/request.json --json
vep lecture render prepared/request.json authored-content.json --destination drafted --json
vep lecture audit-prepare drafted --kind content --json
vep lecture audit-prepare drafted --kind visual --json
vep lecture review drafted --content-audit content-audit.json --visual-audit visual-audit.json --destination review-v001 --json
vep lecture validate review-v001 --json
```

Use an existing destination parent and an absent destination for each new version. Byte-identical inputs
reuse a verified existing stage. Use the next version and a distinct destination to address audit findings.
Authoring and audits are semantic exchanges. Run each audit in a distinct fresh context and provide its frozen
request and source access. Their schema is `lecture-audit-response.schema.json`. The audits return the required
target IDs, evidence-linked findings and execution identity. VEP checks identity, coverage and binding to the
rendered bytes. Any P0 or P1 prevents publication of the review candidate.

The author input uses `lecture-input.schema.json`. The author receives the frozen request and returns
`rich-content.schema.json` with the exact requested output mode and fingerprint. Each cited current or
previous source must exactly match the frozen catalog. Label added explanations as `supplement`. Keep
teaching emphasis, assignments and follow-on references grounded, and mark unanswered coverage gaps.
The versioned profile carries scoped rules, language and optional hashes for the caller's private rule sources.
`worked_explanation` prioritizes understanding and reproducible derivations. `submission_answer` follows
the caller's answer format while preserving evidence and calculation steps. The draft procedures for technical
authoring, figure planning and visual audit remain on the Skill evaluation path.

### Input variants

- An existing VEP workspace must have verified evidence and local processing authorization.
- Use `vep lecture prepare LEGACY_ARCHIVE --archive ...` for a validated approved `study-bundle`. The request
  carries the legacy approved draft SHA so the new review can be compared with the prior result. Archive mode
  is local-only and retains the archive and its existing approval.
- Use `vep lecture prepare SOURCE_INDEX.json --documents ...` for local PDF/image/notes without A/V.
  The index schema is `lecture-document-index.schema.json`. For each file, specify its exact SHA-256.
  Paths can be absolute local paths or portable relative paths resolved beside the index. PDF inputs preserve
  page numbers, raster data and text extraction. Inspect the page image when extracted text is empty or
  ambiguous. Images require visual inspection. They receive no inferred transcription.
- Add `--document-index SOURCE_INDEX.json` to a workspace or archive command to bind supplementary current
  lecture documents to the same frozen request. The index must use the same job ID. Document processing is local.
- For workspace/archive source crops, pass the existing verified private root to `lecture render --assets ROOT`.
  Direct document crops resolve from `prepared/source-assets/`. Source crop packages retain the raster needed
  to rebuild the crop. Include images only under the permitted distribution scope.

### Review contract

`bundle-manifest.json` follows `rich-deliverable-manifest.schema.json`. Its SHA-256 target binds the inventory,
job/version, fixed evidence fingerprint, authoring profile hash and typed content. The inventory covers the
editable MDX/Markdown and semantic model, TeX and MathML, original graph specifications/data, static figures,
standalone HTML, A4 PDF, static site files, renderer/version/hash receipt, and both independent audit responses.
The artifact tree and receipt are verified again before approval. The same semantic content snapshot supplies
each viewer. To rebuild the exact content against the original request in a new directory, pass its `content.json`
to `lecture render`. Pass the bundle's `inputs/` as `--assets` if the package contains source crops.

To edit the portable MDX, copy `lesson.mdx` from a verified render or review package and edit the
copy. Retain its block markers, component structure and provenance references. Prepare the next version and
import the edited title, paragraphs, heading and callout text, displayed equations, step text/calculations and
figure labels. For structural, source-reference, table, figure geometry or data edits, update the typed
content instead. A new render and both audits are required for review of either edit route.

```sh
vep lecture source-import review-v001 edited-lesson.mdx --request prepared-v002/request.json --destination edited-content.json --editor NAME --json
vep lecture render prepared-v002/request.json edited-content.json --destination drafted-v002 --json
```

`review.html` embeds figures and styles. `sites/` contains same-origin static assets and reserves deployment
for the receiving system. The review package and source assets are private. Send or deploy them only through
an authorized destination with the intended access controls.

Only the actual reviewer can record approval through the human CLI:

```sh
vep lecture approve review-v001 --approval-target-sha256 TARGET --reviewer NAME --basis 'Review basis' --confirm --json
vep lecture approval-validate review-v001 --json
```

The approval is written once as the sibling `review-v001.approval.json` for the validated candidate.
Its identity is CLI-asserted. The original review bundle bytes are stable and its manifest continues to describe the
review candidate. Approval and hosting occur through separate operations.

Reading HTML and PDF group citations in numbered section endnotes. The HTML notes expose individual evidence locators and the MDX and structured source retain per-claim bindings. Use equation blocks with aligned lines for symbolic operations; worked numeric steps retain their precision and units.
