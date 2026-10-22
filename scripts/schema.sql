PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

-- One database per work; each document is an immutable source edition.
CREATE TABLE source_document (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, original_path TEXT NOT NULL,
    source_format TEXT NOT NULL, encoding TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL UNIQUE, decoded_sha256 TEXT NOT NULL,
    locator_policy TEXT NOT NULL
);
CREATE TABLE chapter (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES source_document(id),
    source_sequence INTEGER NOT NULL, title TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK(start_char >= 0),
    end_char INTEGER NOT NULL CHECK(end_char > start_char),
    raw_text TEXT NOT NULL CHECK(length(raw_text) = end_char - start_char),
    reading_status TEXT NOT NULL CHECK(reading_status IN ('unread','read','excluded')),
    extraction_status TEXT NOT NULL CHECK(extraction_status IN ('pending','draft','reviewed')),
    coverage_note TEXT NOT NULL DEFAULT '', UNIQUE(document_id, source_sequence)
);
CREATE TABLE passage (
    id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL REFERENCES chapter(id),
    paragraph_start INTEGER NOT NULL,
    paragraph_end INTEGER NOT NULL CHECK(paragraph_end >= paragraph_start),
    start_char INTEGER NOT NULL CHECK(start_char >= 0),
    end_char INTEGER NOT NULL CHECK(end_char > start_char), quote TEXT NOT NULL,
    UNIQUE(chapter_id, start_char, end_char)
);
CREATE TRIGGER passage_exact_source BEFORE INSERT ON passage BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM chapter c WHERE c.id = NEW.chapter_id
        AND NEW.end_char <= length(c.raw_text)
        AND NEW.quote = substr(c.raw_text, NEW.start_char + 1, NEW.end_char - NEW.start_char)
    ) THEN RAISE(ABORT, 'passage must match its immutable source slice') END;
END;

CREATE TABLE entity (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
    scope_note TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
);
-- Narrative content and structured selectors share the same evidence/edit handling.
CREATE TABLE record (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('fact','event','relation','alias','question')),
    title TEXT NOT NULL, content TEXT NOT NULL,
    subject_id TEXT REFERENCES entity(id), predicate TEXT NOT NULL DEFAULT '',
    value_text TEXT NOT NULL DEFAULT '', object_id TEXT REFERENCES entity(id),
    source_status TEXT NOT NULL CHECK(source_status IN
        ('explicit','attributed','interpretation','unresolved')),
    modality TEXT NOT NULL CHECK(modality IN ('actual','planned','general','hypothetical','unknown')),
    story_time TEXT NOT NULL DEFAULT '', scope_note TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'draft'
        CHECK(review_status IN ('draft','agent_checked','human_checked')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
);
CREATE TABLE record_entity (
    record_id TEXT NOT NULL REFERENCES record(id), entity_id TEXT NOT NULL REFERENCES entity(id),
    role TEXT NOT NULL, PRIMARY KEY(record_id, entity_id, role)
);
CREATE TABLE evidence (
    record_id TEXT NOT NULL REFERENCES record(id), passage_id TEXT NOT NULL REFERENCES passage(id),
    kind TEXT NOT NULL CHECK(kind IN ('narration','dialogue','thought','mixed')),
    speaker_id TEXT REFERENCES entity(id), note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(record_id, passage_id)
);
CREATE TABLE record_link (
    from_id TEXT NOT NULL REFERENCES record(id), to_id TEXT NOT NULL REFERENCES record(id),
    relation TEXT NOT NULL CHECK(relation IN
        ('before','motivates','responds_to','clarifies','contradicts','corrects','part_of')),
    note TEXT NOT NULL, PRIMARY KEY(from_id, to_id, relation), CHECK(from_id != to_id)
);
CREATE TABLE article (
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entity(id),
    title TEXT NOT NULL, body TEXT NOT NULL, scope_note TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
);
CREATE TABLE article_record (
    article_id TEXT NOT NULL REFERENCES article(id), record_id TEXT NOT NULL REFERENCES record(id),
    used_version INTEGER NOT NULL CHECK(used_version > 0), PRIMARY KEY(article_id, record_id)
);
CREATE TABLE revision (
    id INTEGER PRIMARY KEY, target_table TEXT NOT NULL, target_id TEXT NOT NULL,
    before_json TEXT, after_json TEXT NOT NULL, reason TEXT NOT NULL, editor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE reading_task (
    pack_id TEXT NOT NULL, task TEXT NOT NULL CHECK(task IN ('events','details')),
    chapter_id TEXT NOT NULL REFERENCES chapter(id),
    assigned_start INTEGER NOT NULL, assigned_end INTEGER NOT NULL,
    result_sha256 TEXT NOT NULL, record_count INTEGER NOT NULL CHECK(record_count >= 0),
    status TEXT NOT NULL DEFAULT 'imported' CHECK(status IN ('imported','reviewed')),
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY(pack_id, task)
);
CREATE VIEW article_staleness AS
SELECT a.id, a.title, COUNT(CASE WHEN r.version != ar.used_version THEN 1 END) AS changed_records
FROM article a LEFT JOIN article_record ar ON ar.article_id = a.id
LEFT JOIN record r ON r.id = ar.record_id GROUP BY a.id;
CREATE VIEW entity_names AS
SELECT id AS entity_id, name, 'canonical' AS kind FROM entity
UNION ALL SELECT subject_id, value_text, 'alias' FROM record
WHERE kind = 'alias' AND source_status = 'explicit';
CREATE INDEX evidence_passage ON evidence(passage_id);
CREATE INDEX record_subject ON record(subject_id, predicate);
CREATE INDEX record_object ON record(object_id);
CREATE INDEX record_entity_entity ON record_entity(entity_id);
CREATE INDEX record_kind_status ON record(kind, source_status, modality);
CREATE INDEX record_link_to ON record_link(to_id);
CREATE TRIGGER source_no_update BEFORE UPDATE ON source_document BEGIN
    SELECT RAISE(ABORT, 'import a new source edition instead of modifying source');
END;
CREATE TRIGGER chapter_no_source_update BEFORE UPDATE OF
    document_id, source_sequence, title, start_char, end_char, raw_text ON chapter BEGIN
    SELECT RAISE(ABORT, 'chapter source is immutable');
END;
CREATE TRIGGER passage_no_update BEFORE UPDATE ON passage BEGIN
    SELECT RAISE(ABORT, 'insert a new evidence passage instead');
END;
