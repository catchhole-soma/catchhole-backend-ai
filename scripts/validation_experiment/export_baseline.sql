-- Read-only baseline export for the local validation experiment (no data in this file).
-- Run in psql against the production database with a read-only role, from a directory you control:
--   psql "$READONLY_URL" -v work_id=<WORK UUID> -v target_no=31 -f export_baseline.sql
-- Output: baseline-raw.json (one JSON object, exportVersion 1). Keep it outside every git repository.
\set ON_ERROR_STOP on
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '120s';

-- (1) sanity / contamination check: run first and keep the result next to the export.
SELECT w.id, w.lifecycle_status,
 (SELECT max(e.episode_no) FROM episodes e WHERE e.work_id=w.id AND e.status<>'ARCHIVED') AS latest_active_episode,
 (SELECT count(*) FROM characters c WHERE c.work_id=w.id AND c.status='ACTIVE') AS characters,
 (SELECT count(*) FROM world_settings s WHERE s.work_id=w.id) AS world_settings,
 (SELECT count(*) FROM world_setting_candidates wc WHERE wc.work_id=w.id AND wc.review_status='CONFIRMED') AS confirmed_world_candidates,
 (SELECT count(*) FROM character_facts f JOIN characters c ON c.id=f.character_id JOIN episodes e ON e.id=f.source_episode_id
   WHERE c.work_id=w.id AND e.episode_no>=:target_no) AS char_facts_from_target_or_later,
 (SELECT count(*) FROM world_setting_candidates wc JOIN episodes e ON e.id=wc.source_episode_id
   WHERE wc.work_id=w.id AND wc.review_status='CONFIRMED' AND e.episode_no>=:target_no) AS world_from_target_or_later
FROM works w WHERE w.id=:'work_id';

-- (2) raw baseline export -> baseline-raw.json
\pset format unaligned
\pset tuples_only on
\o baseline-raw.json
SELECT jsonb_build_object('exportVersion',1,'exportedAt',now(),'workId',:'work_id','targetEpisodeNo',:target_no,
 'episodes',(SELECT jsonb_agg(jsonb_build_object('id',e.id,'episodeNo',e.episode_no,'status',e.status,'contentHash',e.content_hash,
     'contentS3Key',e.content_s3_key,'contentS3Version',e.content_s3_version,'charCount',e.char_count) ORDER BY e.episode_no)
   FROM episodes e WHERE e.work_id=:'work_id' AND e.status<>'ARCHIVED' AND e.episode_no<:target_no),
 'characters',(SELECT jsonb_agg(jsonb_build_object('id',c.id,'name',c.name,'currentAge',c.current_age,'currentLevel',c.current_level,
     'profileJson',c.profile_json,'statsJson',c.stats_json,'skillsJson',c.skills_json,'itemsJson',c.items_json,'statusesJson',c.statuses_json,
     'currentNarrativeTime',c.current_narrative_time,'manuallyEditedSlots',c.manually_edited_slots,'snapshotVersion',c.snapshot_version,
     'createdAt',c.created_at) ORDER BY c.created_at DESC, c.id)
   FROM characters c WHERE c.work_id=:'work_id' AND c.status='ACTIVE'),
 'snapshotSources',(SELECT jsonb_agg(jsonb_build_object('characterId',s.character_id,'factType',s.fact_type,'factKey',s.fact_key,
     'sourceOrder',s.source_order,'factValue',f.fact_value,'sourceEpisodeNo',fe.episode_no)
     ORDER BY s.character_id,s.fact_type,s.fact_key,s.source_order)
   FROM character_snapshot_sources s JOIN character_facts f ON f.id=s.source_fact_id JOIN characters c ON c.id=s.character_id
   LEFT JOIN episodes fe ON fe.id=f.source_episode_id
   WHERE c.work_id=:'work_id' AND c.status='ACTIVE'),
 'discoveries',(SELECT jsonb_agg(jsonb_build_object('id',sc.id,'matchedCharacterId',sc.matched_character_id,'entityName',sc.entity_name,
     'createdAt',sc.created_at,'sourceEpisodeNo',de.episode_no) ORDER BY sc.created_at, sc.id)
   FROM setting_candidates sc LEFT JOIN episodes de ON de.id=sc.episode_id
   WHERE sc.work_id=:'work_id' AND sc.candidate_kind='CHARACTER_DISCOVERY'
     AND sc.review_status='CONFIRMED' AND sc.matched_character_id IS NOT NULL),
 'worldSettings',(SELECT jsonb_agg(jsonb_build_object('id',w.id,'category',w.category,'subjectName',w.subject_name,
     'normalizedSubjectName',w.normalized_subject_name,'propertiesJson',w.properties_json,'manuallyEditedPaths',w.manually_edited_paths,
     'version',w.version,
     'latestSourceEpisodeNo',(SELECT max(e.episode_no) FROM world_setting_candidates wc JOIN episodes e ON e.id=wc.source_episode_id
        WHERE wc.target_world_setting_id=w.id AND wc.review_status='CONFIRMED' AND wc.history_only=false),
     'earliestSourceEpisodeNo',(SELECT min(e.episode_no) FROM world_setting_candidates wc JOIN episodes e ON e.id=wc.source_episode_id
        WHERE wc.target_world_setting_id=w.id AND wc.review_status='CONFIRMED' AND wc.history_only=false)) ORDER BY w.id)
   FROM world_settings w WHERE w.work_id=:'work_id'),
 -- Mirrors the server capture: confirmed history except rows from an episode analysis of the target or later.
 'worldHistory',(SELECT jsonb_agg(jsonb_build_object('candidateId',wc.id,'targetWorldSettingId',wc.target_world_setting_id,
     'category',wc.final_category,'subjectName',wc.final_subject_name,'scopeName',wc.final_scope_name,'settingName',wc.final_setting_name,
     'value',wc.final_value,'operation',wc.final_operation,'historyOnly',wc.history_only,'effectiveFromEpisodeNo',wc.effective_from_episode_no,
     'temporalScope',wc.temporal_scope) ORDER BY wc.reviewed_at, wc.id)
   FROM world_setting_candidates wc LEFT JOIN analysis_jobs j ON j.id=wc.analysis_job_id
   WHERE wc.work_id=:'work_id' AND wc.review_status='CONFIRMED'
     AND NOT (j.id IS NOT NULL AND j.source_kind<>'SETTING_BOOK' AND j.source_episode_no IS NOT NULL AND j.source_episode_no>=:target_no)));
\o
COMMIT;
