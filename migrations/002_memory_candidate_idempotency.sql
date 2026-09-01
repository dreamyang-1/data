-- Makes retries of the same memory-candidate request return one durable record.
-- Apply after 001_long_term_memory.sql. The initial project table is empty when this runs.

ALTER TABLE agent_long_term_memory
    ADD COLUMN candidate_dedupe_key CHAR(64) NOT NULL
        COMMENT 'SHA-256 of scope, source message, memory type and logical key'
        AFTER memory_id,
    ADD UNIQUE KEY uq_ltm_candidate_dedupe (candidate_dedupe_key);
