"""Editorial acceptance after an explicit contextual review; no inference from silence alone."""
from pathlib import Path

FIELDS = {'confirmation_status', 'confirmation_basis', 'confirmed_through', 'confirmation_note'}
SEMANTIC_FIELDS = {'content', 'title', 'kind', 'subject_id', 'object_id', 'predicate',
                   'value_text', 'source_status', 'modality', 'story_time', 'scope_note'}


def migrate(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version == 2:
        return
    if version != 1:
        raise ValueError('Expected database schema v1 or v2')
    sql = Path(__file__).with_name('confirmation.sql').read_text(encoding='utf-8')
    with db:
        db.execute('BEGIN IMMEDIATE')
        for statement in sql.split(';'):
            if statement.strip():
                db.execute(statement)


def assess(db, op, before, fields):
    review = op.get('confirmation')
    changed = (bool(SEMANTIC_FIELDS & fields.keys()) or 'evidence' in op or 'participants' in op
               or fields.get('review_status') == 'draft')
    if not review:
        if changed and before:
            fields.update(confirmation_status='judgment', confirmation_basis='',
                          confirmed_through=None, confirmation_note='内容或证据变更，需重新确认。')
        return
    if before is None:
        raise ValueError('Save evidence-backed draft before confirming it')
    if changed:
        raise ValueError('Save content changes first, then review that version')
    status = review['status']
    note = review.get('note', '')
    if status not in ('judgment','provisional','fact','disputed','rejected') or not note.strip():
        raise ValueError('A valid confirmation status and review note are required')
    if status in ('judgment','disputed','rejected'):
        fields.update(confirmation_status=status, confirmation_basis='',
                      confirmed_through=None, confirmation_note=note)
        return
    if before['kind'] == 'question' or before['source_status'] == 'unresolved':
        raise ValueError('Resolve a question into an evidence-backed statement before promotion')
    if before['review_status'] == 'draft':
        raise ValueError('Check the extracted statement against its source before promotion')
    if not all(review.get(k) is True for k in ('faithful_to_source','no_unresolved_conflict','eligible')):
        raise ValueError('Explicit contextual review is required: faithful_to_source, no_unresolved_conflict, eligible')
    conflicts = db.execute('''SELECT 1 FROM record_link l JOIN record other ON
        other.id=CASE WHEN l.from_id=? THEN l.to_id ELSE l.from_id END
        WHERE ((l.relation='contradicts' AND (l.from_id=? OR l.to_id=?))
        OR (l.relation='corrects' AND l.to_id=?)) AND other.confirmation_status!='rejected' LIMIT 1''',
        (before['id'],)*4).fetchone()
    if conflicts:
        raise ValueError('Resolve recorded contradictions/corrections before promotion')
    basis, through_id = review.get('basis'), review.get('through_chapter')
    through = db.execute('SELECT * FROM chapter WHERE id=?', (through_id,)).fetchone()
    evidence = db.execute('''SELECT DISTINCT c.document_id,c.source_sequence FROM evidence e
        JOIN passage p ON p.id=e.passage_id JOIN chapter c ON c.id=p.chapter_id WHERE e.record_id=?''',
        (before['id'],)).fetchall()
    if not through or not evidence or any(r['document_id'] != through['document_id'] for r in evidence):
        raise ValueError('Review must cover the same source edition as all evidence')
    first, last = min(r['source_sequence'] for r in evidence), max(r['source_sequence'] for r in evidence)
    end = through['source_sequence']
    if end < last:
        raise ValueError('Review ends before the evidence')
    if status == 'provisional' and basis not in ('window','arc_end'):
        raise ValueError('Provisional confirmation requires window or arc_end review')
    if status == 'fact' and basis not in ('full_text','direct'):
        raise ValueError('Fact confirmation requires full_text or direct evidence review')
    if basis == 'window' and end < first + 10:
        raise ValueError('Window review requires at least 10 following catalog chapters')
    if basis == 'arc_end' and not review.get('arc_note', '').strip():
        raise ValueError('Describe which story arc has concluded')
    if basis == 'arc_end':
        note += ' 剧情收束依据：' + review['arc_note']
    if basis == 'full_text':
        if before['confirmation_status'] != 'provisional':
            raise ValueError('Batch full-text acceptance applies to provisional records')
        scope = db.execute('SELECT total_chapters FROM source_scope WHERE document_id=?',
                           (through['document_id'],)).fetchone()
        if not scope or scope[0] is None or end != scope[0]:
            raise ValueError('A complete source catalog and its final chapter are required')
        first = 1
    chapters = db.execute('''SELECT source_sequence,reading_status,coverage_note FROM chapter
        WHERE document_id=? AND source_sequence BETWEEN ? AND ? ORDER BY source_sequence''',
        (through['document_id'], first, end)).fetchall()
    if [r['source_sequence'] for r in chapters] != list(range(first, end+1)) or any(
        r['reading_status'] != 'read' and not (r['reading_status'] == 'excluded' and r['coverage_note'].strip())
        for r in chapters):
        raise ValueError('The review range contains missing or unread chapters')
    fields.update(confirmation_status=status, confirmation_basis=basis,
                  confirmed_through=through_id, confirmation_note=note)
