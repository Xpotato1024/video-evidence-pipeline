# Use VEP from a CLI or an AI client

Run the CLI in a complete VEP checkout. Start with [installation](installation-and-updates.md) and `uv run vep doctor --json`. The checkout includes a synthetic meeting transcript and an informal note. Those inputs require Python and uv. Audio and video processing, PDF input extraction, and browser rendering have their own dependencies.

| What you have | Entry point | What you can save or resume |
| --- | --- | --- |
| Video or audio URL supported by an adapter | `vep job init`, `vep job authorize`, `vep acquire` | Verified source bytes and a job workspace |
| Panopto lecture URL | `vep workflow start` and `vep workflow run` | A transcript by default, or verified source media with `--stop-after acquired` |
| Existing text transcript and one or more local PDFs, images or notes | `vep job init --primary-input transcript` with repeatable `--supporting-input` | Transcript, source locations, evidence and unresolved items |
| Local documents for a technical lecture | `vep lecture prepare --documents` | A checked authoring request with indexed document pages |
| Saved technical manuscript | `vep lecture render` | The selected formats and a receipt bound to the manuscript |

## Authentication handoff

Use `--json` in an AI client. Pass the stable input and a credential reference appropriate for the provider. The acquisition process reads credentials at runtime. The `authentication` object describes the provider, user action and available methods. The `resume` object names the operation and workspace. The human completes SSO or MFA in the indicated browser or follows the provider's token setup on the host. Resume with the workspace and the applicable runtime options. Use `vep job guide WORKSPACE --json` to inspect the next operation for the saved job.

Authentication expiry and denial have different results. The pinned backend retries fragments for native HLS and DASH under bounded limits. A missing fragment fails the transfer and prevents an incomplete recording from reaching transcription. A job with a verified acquired source continues locally without a fresh download. Partial fragments are cleaned up when the transfer fails. See [network acquisition](network-acquisition.md) for supported adapters and their operational limits.

## Generate meeting minutes from synthetic inputs

Discover the preset IDs and output formats with `uv run vep presets --json`. The presets `summary`, `meeting-minutes` and `report` reuse the verified evidence workflow. The lecture path takes its own structured request and offers `worked_explanation` and `submission_answer`.

Run the following from the root of the checkout. The commands use an unused job ID on the first run.

```sh
uv run vep job init sample-meeting --source-kind local_file \
  --source "$PWD/examples/meeting/transcript.txt" --primary-input transcript \
  --supporting-input "notes:$PWD/examples/meeting/notes.txt" \
  --preset meeting-minutes --language en --output-format markdown \
  --purpose 'Record the decision, action owners, deadlines, and open items' --json
uv run vep job authorize .local/jobs/sample-meeting --basis 'Public synthetic example' \
  --confirm --allow-local-processing --json
uv run vep acquire .local/jobs/sample-meeting --json
uv run vep normalize .local/jobs/sample-meeting --json
uv run vep transcribe .local/jobs/sample-meeting --json
uv run vep job guide .local/jobs/sample-meeting --plan --json
```

Continue via [the AI exchange](ai-exchange.md). Each semantic preparation step returns the request, instructions and response schema for the AI client. Pass through refinement, evidence extraction and authoring, then send the draft and evidence to the independent auditor. The example has an agreed one-workshop pilot, a form draft assigned to Riku for 27 November, and an open question about who collects the forms. The note's tentative suggestion about Hana is relevant to the open question and carries the note's provenance. A local AI can run the exchange without a service account.

## Resolve uncertainty and choose an output

Use `vep evidence uncertainties WORKSPACE --json` to read the remaining questions and earlier clarifications. The underlying evidence and each new clarification retain their own provenance. An informal note can identify where to look, express a tentative hypothesis or document an explicit follow-up. The author weighs it against the sources relevant to that specific claim.

Record a verified clarification with `vep evidence clarify WORKSPACE --response follow-up.json --operator NAME --confirm --json`. Use the [clarification schema](../../schemas/clarification-input.schema.json), the evidence fingerprint and an ambiguity ID from `uncertainties`. For a generic document, `vep job recover WORKSPACE follow-up --with-clarifications --basis 'Recorded clarification for a new draft' --confirm --json` reuses the transcript and makes the recorded follow-up available to a fresh evidence run. For a new technical lecture request, use `vep lecture prepare --with-clarifications`. Earlier requests remain pinned to their original inputs.

Use the educational supplement mechanism for a derivation reconstructed from stated assumptions. Attribute a group's decision, owner or approval to the applicable records or a recorded confirmation. A correct derivation and evidence of what was actually said remain separate. Continue drafting unaffected blocks while a material question is open.

After the human approves a summary, set of minutes or report, export just the format needed. These commands target the same approved Markdown source.

```sh
uv run vep export WORKSPACE --format markdown --json
uv run vep export WORKSPACE --format html --destination delivery-html --json
uv run vep export WORKSPACE --format pdf --destination delivery-pdf --json
```

For a lecture, choose one or more outputs independently of the authoring mode.

```sh
uv run vep lecture render prepared/request.json content.json \
  --format html --format pdf --destination review-render --json
```

The selected lecture outputs determine the files bound by review and approval. A new output set uses its own destination. The saved request and manuscript can be reused. The lecture guide covers [inputs, audits and approval](technical-lecture.md).
