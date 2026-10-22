-- Schema v2: editorial acceptance is independent from source attribution.
ALTER TABLE record ADD COLUMN confirmation_status TEXT NOT NULL DEFAULT 'judgment'
    CHECK(confirmation_status IN ('judgment','provisional','fact','disputed','rejected'));
ALTER TABLE record ADD COLUMN confirmation_basis TEXT NOT NULL DEFAULT ''
    CHECK(confirmation_basis IN ('','window','arc_end','full_text','direct'));
ALTER TABLE record ADD COLUMN confirmed_through TEXT REFERENCES chapter(id);
ALTER TABLE record ADD COLUMN confirmation_note TEXT NOT NULL DEFAULT '';
CREATE TABLE source_scope (
    document_id TEXT PRIMARY KEY REFERENCES source_document(id),
    total_chapters INTEGER CHECK(total_chapters > 0),
    note TEXT NOT NULL DEFAULT ''
);
DROP VIEW entity_names;
CREATE VIEW entity_names AS
SELECT id AS entity_id, name, 'canonical' AS kind FROM entity
UNION ALL SELECT subject_id, value_text, 'alias' FROM record
WHERE kind='alias' AND (source_status='explicit' OR confirmation_status='fact')
AND confirmation_status NOT IN ('disputed','rejected');
PRAGMA user_version = 2;
