# CtxFlow memory demo corpus

The checked-in corpus at
[`data/ctxflow_demo_corpus.jsonl`](../data/ctxflow_demo_corpus.jsonl) is a
small deterministic workload for the Layer 1 memory substrate. It exercises
linked concepts, isolation, provenance, auditability, retrieval, supersession,
compaction, governance, backup, and observability vocabulary without requiring
network access or a database.

Run the ingestion and statistics report from the repository root:

```bash
python scripts/demo_corpus.py
```

The command reports:

- `corpus_records`, `nodes`, and `edges`: graph topology;
- `tag_assignments`: supplied tag coverage;
- `content_bytes`: UTF-8 size of source content;
- `serialized_bytes`: JSON persistence footprint;
- `serialized_bytes_per_node`: serialized footprint normalized by node count.

The report intentionally uses logical and serialized footprint metrics rather
than process RSS. RSS varies with the Python allocator and operating system,
while these values are reproducible from the corpus and graph serialization.
The graph uses generated node IDs, but IDs have fixed-width output, so the
serialized size remains stable for this corpus and configuration.
