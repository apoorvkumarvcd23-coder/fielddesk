# Claim-to-code/test ledger

CORE02 indexing and the CORE05 identity/RAG API slice are implemented. Final
FieldDesk core acceptance still requires the operator/review workflow. No
core/release tag is earned by these slices.

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
| Seeded identities use hashed random bearer tokens; role comes from the DB | `rag.py`, migration 3, `rag_manage.py` | Missing/invalid/legacy tokens, client impersonation and disabled identity checks |
| Explicit grants filter before similarity search and prompting | `EVIDENCE_SQL`, `RagService.retrieve` | Cross-owner/wrong-space source denials, no implicit operator/admin read, captured deterministic prompt evidence |
| Current grants/enabled state and ready versions are checked at assembly | `RagService.permitted/answer` | Revocation of identity/document/grant and version replacement during generation all suppress output |
| Local query embeddings use search_query; generation has fixed budgets and no tools | `embeddings.py`, `generation.py` | Transport/prefix/budget checks; actual model/digest/token counters in local report |
| Unknown/duplicate IDs or fabricated quotes cannot become cited answers | `RagService.answer` | Invalid structured output and fabricated quote tests; exact Unicode offsets |
| Trace access is owner-scoped; prompts/queries/quotes are not persisted in traces | `rag_traces`, `/rag/traces/{id}` | Cross-identity denial and trace-content assertions |
| Actual learned model answers and fails closed on unsupported/outage/timeout | `demo_rag.py` | `local-rag-2026-11-11.json`: actual nomic/qwen3.5 inference on SQLite, explicitly not pgvector |

Deterministic CI checks both SQLite and real PostgreSQL. The separate
`actual-model-rag` CI job downloads public nomic/qwen3.5:9b models and runs
`demo_rag.py --postgres`; only a successful job/report proves that particular
Linux/CPU pgvector model integration. The local qwen3.5:9b report proves actual
inference with SQLite and must not be relabelled as PostgreSQL evidence.
Their small synthetic workload does not establish retrieval accuracy, answer
grounding, throughput, production scale or customer use.

## Remaining core gates

- Operator ingest/job/review interface, unanswered review queue and complete
  approved-ticket end-to-end scenario.
- Full clean-clone app/worker/model setup, screenshots and complete core acceptance.

The new identity/document matrix is separate from legacy shared-key route denials.
Use the [latest CI result and actual-model artifact](https://github.com/apoorvkumarvcd23-coder/fielddesk/actions/workflows/tests.yml)
for PostgreSQL verification; a failed run leaves that gate open. Local Docker
could not start on 11 November; the SQLite report is not PostgreSQL evidence.
The generation readiness probe alone does not prove a cited RAG answer. See the
implementation plan for the later core and December evaluation/release gates.
