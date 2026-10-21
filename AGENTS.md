# Project agent instructions

## Explicit commands before actions

- Do not implement or change anything without an explicit instruction from the
  human user to perform that action. A question about feasibility, causes,
  alternatives, or recommendations is a request for an answer, not permission
  to make changes, even when the intended improvement seems obvious.
- Questions such as "Can this be done?", "Could we make this one block?", or
  "What could we do about this?" authorize discussion only. Explain the options
  and wait for a direct command or unambiguous confirmation before acting.
- Read-only inspection needed to answer the actual question is allowed. Do not
  use it as a pretext to start implementation, regenerate outputs, launch an
  audit/subagent, install dependencies, or change external state.
- A direct implementation command covers necessary in-scope implementation and
  verification steps, not additional features or unrelated fixes. When intent
  or scope is unclear, ask before taking a state-changing action.
- Permission to audit is not permission to fix. Permission to edit is not
  permission to commit; permission to commit is not permission to push.
- Apply this boundary to the coordinating agent and every subagent. Include it
  in delegated tasks so a reviewer cannot silently turn findings into fixes.

## Language and scope

- Communicate with the human user in Russian unless they request another language.
- Write all subagent task prompts, follow-up messages, progress summaries, and
  reports in English. Preserve source quotations verbatim in their original
  language. The coordinating agent presents the findings to the user in Russian.
- These rules apply to every subagent working on this project, not just auditors.
- OCR and a web interface are outside the project's scope. Translation is an
  optional, separate Gemini API stage; PDF conversion must remain local and
  network-free. Gemini is the go-to provider for the first integration.
- Keep translation provider-independent (DIP): the core depends on translation
  ports, never a concrete SDK, prompt or wire format. Select adapters in the
  composition root; namespace checkpoints by provider identity and revision.
- Auto routing uses a dynamic Gemini catalog and a project-local ignored
  `model-availability.sqlite3` negative cache. Never persist keys, source text,
  translations or raw provider errors in that database. Preserve per-segment
  model attribution in the separate translation journal when routing changes.
- Do not send user documents in live API requests merely to implement or test
  translation. Use offline mocks unless the user explicitly requests a live
  translation/test. Never print, commit, or include API keys in reports or HTML.
- Do not commit or push unless the user explicitly requests it.
- Never commit or force-add user example PDFs or their conversion/translation
  artifacts. Keep the example collection under ignored `example/` (legacy
  `example.pdf` is also ignored), and generated outputs/journals/reports under
  ignored `output/` or `tmp/`. Local HTML documentation remains ignored too.
- Response-validation recovery retries the whole unchanged batch on the same
  model; never split it or concatenate partial answers to repair an invalid
  response. Exhausted response retries isolate that batch and produce an
  incomplete result; other independent batches may continue. MAX_TOKENS is not
  a response-retry or fallback signal. Keep the pre-existing input-token guard
  separate from this policy. Strict and explicit legacy Gemini contracts have
  distinct identities; never silently migrate or discard an old journal.

## Documentation maintenance before commits

- Before every user-authorized commit, review the entire documentation set for
  impact. If the change is significant for understanding, using, developing, or
  checking the project, update every affected document before committing. Do not
  limit the review to README.md or leave conflicting descriptions elsewhere.
- Significant changes include behavior, CLI/API contracts, models, algorithms,
  file/module organization, dependencies, warnings, limitations, tests, workflows,
  and project instructions. Small internal changes may need no prose updates;
  still assess their impact rather than rewriting unrelated documentation.
- Keep the offline HTML documentation at `output/documentation.html` synchronized
  with the current Markdown documentation. Rebuild it when documentation changes
  and when changes affect source files embedded in that HTML snapshot. Do not
  manually patch generated HTML instead of updating its sources and rebuilding.
- Follow the documentation maintenance and HTML verification steps in
  [the development guide](docs/development.md). Check internal links, mobile
  rendering, and snapshot freshness. If the local builder or verification tools
  are unavailable, restore the required tooling or report the blocker; do not
  silently leave the HTML stale or claim it was verified.
- Documentation maintenance is part of an authorized change/commit workflow,
  not permission to commit, push, run an independent audit, or regenerate PDF
  conversion outputs. Keep existing Git-ignore boundaries for generated files;
  updating the local HTML does not mean force-adding it to Git.

## Independent PDF conversion audits

Only launch an audit subagent when the user requests an independent/delegated
audit. Writing audit instructions, asking how audits work, or changing the
converter is not a request to run one.

Before delegation, read [the auditor instructions](docs/agents/pdf-quality-auditor.md)
in full and resolve the inputs:

- Absolute paths to the source PDF, Markdown, and TXT to inspect. For the current
  example, use `example.pdf`, `output/example.md`, and `output/example.txt` under
  this repository, unless the user selects other artifacts. Verify they exist.
- Physical, 1-based PDF page numbers and the pages represented by each output.
  A request without a narrower range covers the whole supplied output/document.
- A new run ID, an unused report path under `output/audits/<run-id>/report.md`,
  and a separate scratch directory under `tmp/pdfs/audits/<run-id>/`.
- Input SHA-256 hashes and current Git revision/dirty state. These identify the
  inspected snapshot; a Git revision alone does not identify generated outputs.
- Available generation options, hyphenation decisions, and warning logs. Mark
  unknown values as unknown; do not invent or silently regenerate them.

Spawn one fresh subagent without inherited conversation history when supported
(`fork_context=false`). Send an English task containing the resolved inputs and
the complete auditor instructions, or explicitly require the agent to read that
file in full before starting. Inherit the current model unless the user requests
another one. Do not send previous claims of success, expected findings, or old
audit conclusions. Do not add other PDFs or large-book benchmarks to the scope.

The auditor may create only its assigned report and scratch artifacts. It must
not edit the source, conversion outputs, code, tests, or Git state. This is a
task restriction, not a claim that instructions create a separate OS sandbox.
Do not spawn nested reviewers without additional user authorization.

Collect the report, verify that input hashes are unchanged, and inspect the
evidence for material findings before presenting a Russian summary. Distinguish
confirmed defects, uncertain cases, and coverage gaps. Link the English report.
Audit authorization does not authorize fixes, output regeneration, or commits.

The Markdown file in `docs/agents/` is a project playbook, not an automatically
registered custom agent. The coordinator must load it when launching the audit.
