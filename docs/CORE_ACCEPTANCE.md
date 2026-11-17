# Core acceptance — 17 November 2026

This audit covers the bounded **v0.1.0 core**, with extensions and full v1.0.0
hardening continuing through December. The clean checkout audited here is
`b13a370c5dd26992b54692bbbb47c611b88929b9`. The release tag is created only after the reviewed acceptance
commit also passes its complete clean-checkout CI. Exact immutable tag/commit/CI
links are recorded in the [portfolio release ledger](https://github.com/apoorvkumarvcd23-coder/apoorvkumarvcd23-coder/blob/main/docs/CORE_RELEASES_2026-11-18.md).
Native audit observations are dated 17 November; publication and release checks
carried forward to 18 November. Commit and tag timestamps use their actual time.
No version tag substitutes for the checks.

## Architecture and contract ledger

Identity UI → FastAPI → document-granted pgvector retrieval → local Ollama/citation validation → opted-in review → human-approved persisted ticket; separate leased index worker.

| Core contract | Implementation | Actual verification |
| --- | --- | --- |
| Versioned corpus and nonduplicating durable index | knowledge.py; storage.py migrations; index_worker.py | Replay/lease fencing/rollback/version replacement tests; actual learned worker crash exit 23/reclaim attempt 2 |
| Actual embedding, local generation and pgvector | embeddings.py; generation.py; rag.py EVIDENCE_SQL | Actual nomic 768-dimensional/Qwen3.5:9b Linux pgvector reports; native SQLite report explicitly separate |
| Permission boundaries before prompting and at assembly | rag.py authenticate/retrieve/permitted/answer | 12 test_denial_* parameterizations in the 61-test suite, including grant/version/identity revocation; actual restricted-source HTTP 404 |
| UI ingest → ready → answer → handoff → review → approved ticket | workspace.js; app.py; reviews.py | Actual Chromium/model workflow, explicit consent and human proposal/approval; concurrent approval/rollback and owner tests |
| Model failures and full app/worker restart | demo_workflow.py; workflow_server.py; demo_indexing.py | Real closed generation connection and 1ms deadline; four distinct app PIDs/zero exits; exact persisted state/new cited answer |
| Original 16-case diagnostic and bounded quality scope | evaluate_retrieval.py; fixtures/retrieval_cases.json | Exact parsed baseline unchanged: 9/10 top-1, 4/6 unsupported handoffs, 18/18 source quotes; all 16 results retained |

See the [detailed claim-to-code/test ledger](CLAIM_EVIDENCE.md) and
[full workflow/runbook](OPERATOR_WORKFLOW.md). The [machine-readable audit](core-acceptance-2026-11-17.json)
records native observations and hashes of the existing public evidence, retaining
their platform/model distinction. The [previous verified CI](https://github.com/apoorvkumarvcd23-coder/fielddesk/actions/runs/43754491373)
is source-grounded evidence; fresh release-head CI must pass before tagging.

[Actual native acceptance screenshot](evidence/2026-11-17/core-acceptance.png)

## Reproduce from a clean clone

Install the prerequisites in README and follow the full runbook before these
commands. Use a new ignored artifact directory and synthetic/public data only.
Keep generated account/config files private. All service demos use disposable
owned local services; no paid service or external account is required.

```sh
python -m pip install -r requirements-dev.txt
npm ci --prefix browser
# Install Chromium from browser/ and install the public Ollama models explicitly.
python demo_workflow.py --postgres --artifacts artifacts/core-acceptance
python demo_indexing.py --backend ollama --postgres --output artifacts/worker-restart.json
python evaluate_retrieval.py
```

The native audit used actual installed dependencies and fresh disposable data.
Its platform is recorded explicitly; native fallback does not prove a different
database/service backend. Linux CI executes the documented real-service path.

## Defensible project wording

These are permitted implementation claims for this core, not a personal resume
file or a claim of customer/employer use:

- Built versioned document ingestion and a 768-dimensional pgvector index with leased workers, fenced writes and atomic index publication.
- Implemented document-granted local-model answers, validated source citations, opt-in review and human-approved tickets; verified real model workflows and separate app/worker restarts.

## Remaining scope and limits

- SQLite/hash baseline is separate from learned pgvector/local-model evidence.
- A matched citation quote does not prove answer relevance or general accuracy. Small synthetic demos are not held-out evaluation.
- The 40-case development/held-out evaluation, broader quotas/retention and full December quality/hardening gates remain open.
- Tickets are local persisted workflow records, not an external production ticket provider.
