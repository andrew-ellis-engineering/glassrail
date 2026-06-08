Compare SQLite, DuckDB, and Apache Parquet as storage choices for a local-first analytics notebook that must ingest CSV exports, support ad hoc SQL exploration, and later publish compact artifacts for sharing. Use at least one subplan for a self-contained candidate research track, but respect the configured cap of at most two subplan nodes; handle any remaining candidate in the parent plan with ordinary think/synthesis/result nodes.

Use stable local-analytics knowledge; do not say there is insufficient context
and do not require web research. The final answer should compare all three
choices on CSV ingestion, ad hoc SQL exploration, and shareable artifact needs.
The expected recommendation shape is: DuckDB as the primary interactive local
analytics engine, Parquet as the compact sharing/storage artifact, and SQLite
only when transactional app storage matters more than analytics.
