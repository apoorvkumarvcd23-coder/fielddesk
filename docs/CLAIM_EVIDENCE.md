# Claim-to-code/test ledger

CORE02 is the versioned indexing foundation, not the complete FieldDesk core.
No core/release tag is earned by this slice.

| Implemented behavior | Code | Evidence |
| --- | --- | --- |
| Old documents/tickets survive adoption and repeated checked migrations | `storage.py` | Legacy SQLite adoption, selected-backend upgrade and concurrent-startup tests in `tests/test_knowledge.py` |
| Immutable versions, exact Unicode source offsets and stable replay | `knowledge.py`, `app.py` knowledge routes | Chunk boundaries, API source spans, replay/version tests |
| Separate model/digest/dimension/chunking spaces | `embeddings.py`, `embedding_spaces` / `chunk_embeddings` constraints | Space separation and DB dimension-boundary tests; actual readiness JSON |
| Transactional claims, bounded attempts and lease fencing | `KnowledgeStore.claim/fail/publish` | Concurrent workers, expired/crashed lease and stale-worker tests |
| Failed publication leaves no partial new index or changed ready pointer | `KnowledgeStore.publish`, `Database.connect` | Injected second-vector database failure/rollback and invalid later embedding batch tests |
| Newer desired versions cannot be overwritten by an old completion | `index_targets`, conditional publication | Late old-worker and replacement/replay tests |
| Separate worker process survives crash/restart and exact replay | `index_worker.py`, `demo_indexing.py` | CI hash demos; actual PostgreSQL/Ollama demo JSON, two attempts on first job |
| Operator-only ingestion, job/document/source metadata, no API lease token | `app.py` | Unauthorized/user denial assertions and exact source/status API test |
| Real 768-dimensional embeddings persist in native pgvector storage | `OllamaEmbedder`, `demo_indexing.py` | [`indexing-demo-2026-11-09.json`](indexing-demo-2026-11-09.json), vector_dims min/max both 768 |
| Local generation model can perform bounded inference | `probe_local_models.py` | [`model-readiness-2026-11-09.json`](model-readiness-2026-11-09.json), fixed synthetic prompt and actual digest/counts |
| Existing ticket workflow and 16-case diagnostic retained | Existing API, `evaluate_retrieval.py` | Original tests plus exact comparison against `retrieval-baseline.json` |

Automated CI uses deterministic/mocked adapters and checks both SQLite and real
PostgreSQL. Only the separately recorded local-model runs prove actual inference.
Their small synthetic workload does not establish retrieval accuracy, answer
grounding, throughput, production scale or customer use.

## Remaining core gates

- Bind requests to seeded identities and filter document grants before retrieval;
  eight identity/document permission-denial cases, revocation and citation checks.
- Connect learned pgvector retrieval and local generation to chat; validate source/
  chunk IDs and supporting quotes; show actual model outage/timeout behavior.
- Operator ingest/job/review interface, unanswered review queue and complete
  approved-ticket end-to-end scenario.
- Full clean-clone app/worker/model setup, screenshots and complete core acceptance.

Shared-key route denials do not complete the identity/document permission gate.
The generation readiness probe does not prove a cited RAG answer. See the
implementation plan for the later core and December evaluation/release gates.
