-- Long-term memory source of truth for DataAnalysis_Agent.
-- Stores only durable preference/context candidates and confirmed records.
-- Raw questions, chat transcripts, SQL result rows and analysis results must not be stored here.

CREATE TABLE IF NOT EXISTS agent_long_term_memory (
    memory_id          CHAR(32)      NOT NULL COMMENT 'Application-generated UUID hex',
    tenant_id          VARCHAR(64)   NOT NULL COMMENT 'Mandatory tenant isolation key',
    user_id            VARCHAR(128)  NOT NULL COMMENT 'Mandatory user isolation key',
    application_id     VARCHAR(100)  NOT NULL COMMENT 'Mandatory application isolation key',
    memory_type        VARCHAR(32)   NOT NULL COMMENT 'Durable memory category',
    memory_key         VARCHAR(128)      NULL COMMENT 'Logical key used to supersede an old preference',
    summary_text       VARCHAR(2000) NOT NULL COMMENT 'Short, safe-to-display summary',
    memory_value       JSON          NOT NULL COMMENT 'Structured preference/context only',
    status             VARCHAR(16)   NOT NULL COMMENT 'candidate/active/superseded/deleted',
    confidence         DECIMAL(5,4)  NOT NULL COMMENT 'Extraction confidence, range 0..1',
    source_session_id  VARCHAR(128)  NOT NULL COMMENT 'Conversation that produced the candidate',
    source_message_id  VARCHAR(128)  NOT NULL COMMENT 'Message that produced the candidate',
    created_by         VARCHAR(128)      NULL,
    confirmed_by       VARCHAR(128)      NULL,
    deleted_by         VARCHAR(128)      NULL,
    valid_from         DATETIME(6)   NOT NULL COMMENT 'UTC',
    expires_at         DATETIME(6)       NULL COMMENT 'UTC; NULL means no scheduled expiry',
    superseded_by      CHAR(32)          NULL,
    created_at         DATETIME(6)   NOT NULL COMMENT 'UTC',
    updated_at         DATETIME(6)   NOT NULL COMMENT 'UTC',
    confirmed_at       DATETIME(6)       NULL COMMENT 'UTC',
    deleted_at         DATETIME(6)       NULL COMMENT 'UTC',
    version            INT UNSIGNED  NOT NULL DEFAULT 1 COMMENT 'Optimistic audit version',
    PRIMARY KEY (memory_id),
    KEY idx_ltm_scope_status (
        tenant_id, user_id, application_id, status, expires_at, updated_at
    ),
    KEY idx_ltm_scope_key (
        tenant_id, user_id, application_id, memory_type, memory_key, status
    ),
    KEY idx_ltm_source (
        tenant_id, user_id, application_id, source_session_id, source_message_id
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Confirmed and candidate long-term agent memory; no raw analysis data';
