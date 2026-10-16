-- ============================================================================
-- 长期记忆的本地存放结构（单机、离线、纯文本）
--
-- 设计要点：
--   1. 只有一张主表 memory_record，会话内原文用 layer='SESSION' 表达，
--      这样 clear_session 与 clear_long_term 共用同一套软删除语义。
--   2. 全文检索用 SQLite 自带的全文索引表 memory_fts，分词器取 trigram，
--      实测对比与理由写在 runtime/memory/retrieval.py 顶部注释里。
--   3. 删除一律是软删除（active=0 + deleted_at），所以 restore_character
--      能把某个人物名下被清掉的内容全部还原。彻底清除只在 purge() 里做。
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_record (
    memory_id       TEXT PRIMARY KEY,
    character_id    TEXT NOT NULL DEFAULT 'default',
    -- 契约允许的取值：PROFILE|PERSONA|CANON|RELATIONSHIP|LONG_TERM_MEMORY|SESSION|SOURCE
    layer           TEXT NOT NULL DEFAULT 'LONG_TERM_MEMORY',
    -- preference|fact|relationship|commitment|goal|shared_event|turn
    kind            TEXT NOT NULL DEFAULT 'fact',
    text            TEXT NOT NULL,
    -- 归一化后的文本，专门用于查重（去空白、去标点、全角转半角、小写）
    norm_text       TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.5,
    -- 用户是否明说过「记住…」「以后…」「从现在开始…」
    explicit_flag   INTEGER NOT NULL DEFAULT 0,
    source_session  TEXT NOT NULL DEFAULT '',
    source_turn     INTEGER NOT NULL DEFAULT 0,
    -- CHAT|MANUAL|IMPORT|CANON_SYNC：一条记录是从哪来的，决定它能不能被覆盖
    source_kind     TEXT NOT NULL DEFAULT 'CHAT',
    user_editable   INTEGER NOT NULL DEFAULT 1,
    active          INTEGER NOT NULL DEFAULT 1,
    deleted_at      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS memory_active_idx
    ON memory_record (character_id, active, layer, confidence DESC);
CREATE INDEX IF NOT EXISTS memory_session_idx
    ON memory_record (character_id, source_session, active);
CREATE INDEX IF NOT EXISTS memory_norm_idx
    ON memory_record (character_id, norm_text);

-- 全文索引：外部内容表，正文只存一份，不在索引里再复制一遍
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text,
    content='memory_record',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS memory_fts_ai AFTER INSERT ON memory_record BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_ad AFTER DELETE ON memory_record BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_au AFTER UPDATE OF text ON memory_record BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- 每次写入/更新的流水，用于事后追查「这条是谁改的」
CREATE TABLE IF NOT EXISTS memory_journal (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    action      TEXT NOT NULL,
    memory_id   TEXT NOT NULL,
    character_id TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------------
-- 结构版本（任务 §13：人物目录或数据库必须有 schema_version，未来可以迁移）
-- 只放键值，不放业务数据；读写口在 service.schema_version() / service.meta_value()。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------------
-- 每一场的滚动摘要（任务 §17 的 session summary、§19 的「不许每轮复述」）
--
-- 为什么要单独一张表：摘要描述的是「这一场聊到哪了」，它不属于任何一条记忆，
-- 也不该被 search() 当成事实召回（否则会越召回越啰嗦）。clear_session 会连着
-- 它一起清，clear_long_term 不碰它 —— 与 memory_record 的分层语义保持一致。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_summary (
    character_id  TEXT NOT NULL DEFAULT 'default',
    session_id    TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT '',
    from_turn     INTEGER NOT NULL DEFAULT 0,
    to_turn       INTEGER NOT NULL DEFAULT 0,
    turn_count    INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (character_id, session_id)
);
CREATE INDEX IF NOT EXISTS session_summary_time_idx
    ON session_summary (character_id, updated_at);
