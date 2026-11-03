-- Optional read-only RAG export (Option B; the harness defaults to local re-embedding, Option A). No data here.
--   psql "$READONLY_URL" -v work_id=<WORK UUID> -v target_no=31 -f export_rag.sql
-- Output: rag-raw.json. Keep it outside every git repository. Check readiness first: ready must equal chunks.
\set ON_ERROR_STOP on
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '120s';

SELECT count(*) FILTER (WHERE c.status='READY' AND c.embedding IS NOT NULL) AS ready, count(*) AS chunks,
       count(DISTINCT s.episode_id) AS sources
FROM retrieval_sources s JOIN episodes e ON e.id=s.episode_id LEFT JOIN retrieval_chunks c ON c.episode_id=s.episode_id
WHERE s.work_id=:'work_id' AND e.episode_no<:target_no AND e.status<>'ARCHIVED';

\pset format unaligned
\pset tuples_only on
\o rag-raw.json
SELECT jsonb_build_object(
 'sources',(SELECT jsonb_agg(jsonb_build_object('episodeId',s.episode_id,'sourceKey',s.source_key,'sourceVersion',s.source_version,
     'sourceHash',s.source_hash,'chunkPolicy',s.chunk_policy,'chunkCount',s.chunk_count) ORDER BY s.episode_id)
   FROM retrieval_sources s JOIN episodes e ON e.id=s.episode_id WHERE s.work_id=:'work_id' AND e.episode_no<:target_no AND e.status<>'ARCHIVED'),
 'chunks',(SELECT jsonb_agg(jsonb_build_object('id',c.id,'episodeId',c.episode_id,'chunkIndex',c.chunk_index,'chunkText',c.chunk_text,
     'startOffset',c.start_offset,'endOffset',c.end_offset,'chunkHash',c.chunk_hash,'tokenCount',c.token_count,'embedding',c.embedding::text,
     'embeddingModel',c.embedding_model,'embeddingVersion',c.embedding_version,'status',c.status) ORDER BY c.episode_id,c.chunk_index)
   FROM retrieval_chunks c JOIN retrieval_sources s ON s.episode_id=c.episode_id JOIN episodes e ON e.id=c.episode_id
   WHERE s.work_id=:'work_id' AND e.episode_no<:target_no AND e.status<>'ARCHIVED'));
\o
COMMIT;
