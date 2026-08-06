---
applyTo: "**/*.md,docs/**/*.yaml"
---
Read `metadata_mode` from `docs/registry/documents.yaml` before editing a registered document.
Preserve the minimal semantic front matter for `front-matter` documents and do not add generic front
matter to `registry-only` documents. Git commits and PR reviews are the source of truth for authorship,
edit time, and review. Keep vendor-required front matter such as `applyTo` in `*.instructions.md`.
Historical documents are non-authoritative.
