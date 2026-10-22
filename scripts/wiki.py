"""Evidence-backed fiction Wiki storage. Standard library only; no model API calls."""
import argparse
import json
import re
import sqlite3
from pathlib import Path
from confirmation import migrate, assess

ROOT = Path(__file__).resolve().parent
EDITABLE = {
    'entity': {'name', 'kind', 'scope_note'},
    'record': {'kind', 'title', 'content', 'subject_id', 'predicate', 'value_text',
               'object_id', 'source_status', 'modality', 'story_time', 'scope_note', 'review_status'},
    'article': {'entity_id', 'title', 'body', 'scope_note'},
}
LABELS = {'explicit': '原文明示', 'attributed': '人物说法/视角',
          'interpretation': '整理者推断', 'unresolved': '待查',
          'actual': '已经发生', 'planned': '方案/建议', 'general': '一般描述',
          'hypothetical': '假设', 'unknown': '尚未确定'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def connect(path, readonly=False):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f'Database does not exist: {path}')
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    db.execute('PRAGMA busy_timeout = 5000')
    return db


def paragraphs(text):
    return list(re.finditer(r'[^\r\n]+', text))


def body_start(chapter):
    lines = paragraphs(chapter['raw_text'])
    return int(bool(lines) and lines[0].group().strip() == chapter['title'].strip())


def insert(db, table, fields):
    columns = ','.join(fields)
    placeholders = ','.join('?' for _ in fields)
    db.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(fields.values()))


def snapshot(db, table, identifier):
    if table not in EDITABLE:
        raise ValueError('Only entity, record and article are editable')
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (identifier,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    if table == 'record':
        data['evidence'] = [dict(r) for r in db.execute('''
            SELECT p.chapter_id, p.paragraph_start, p.paragraph_end, e.kind, e.speaker_id, e.note,
                   p.id AS passage_id, p.start_char, p.end_char, p.quote
            FROM evidence e JOIN passage p ON p.id=e.passage_id
            WHERE e.record_id=? ORDER BY p.chapter_id, p.start_char''', (identifier,))]
        data['participants'] = [dict(r) for r in db.execute(
            'SELECT entity_id,role FROM record_entity WHERE record_id=? ORDER BY entity_id,role',
            (identifier,))]
    if table == 'article':
        data['records'] = [dict(r) for r in db.execute(
            'SELECT record_id,used_version FROM article_record WHERE article_id=? ORDER BY record_id',
            (identifier,))]
    return data


def get_passage(db, source):
    chapter = db.execute('SELECT * FROM chapter WHERE id=?', (source['chapter_id'],)).fetchone()
    if chapter is None:
        raise ValueError('Unknown evidence chapter')
    lines = paragraphs(chapter['raw_text'])
    first, last = source['paragraph_start'], source['paragraph_end']
    if not 0 <= first <= last < len(lines):
        raise ValueError('Evidence paragraphs out of bounds')
    start, end = lines[first].start(), lines[last].end()
    identifier = f"{chapter['id']}:p{first:03}-{last:03}"
    if not db.execute('SELECT 1 FROM passage WHERE id=?', (identifier,)).fetchone():
        insert(db, 'passage', dict(id=identifier, chapter_id=chapter['id'],
               paragraph_start=first, paragraph_end=last, start_char=start, end_char=end,
               quote=chapter['raw_text'][start:end]))
    return identifier


def apply_operation(db, op, reason, editor):
    table, identifier = op['table'], op['id']
    if table not in EDITABLE:
        raise ValueError('Unsupported editable table')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
        raise ValueError('Stable IDs must contain only ASCII letters, digits, _ and -')
    if 'expected_version' not in op:
        raise ValueError('expected_version is required; use null for a new object')
    before = snapshot(db, table, identifier)
    expected = op['expected_version']
    if (before is None and expected is not None) or (before is not None and before['version'] != expected):
        raise ValueError(f'Version conflict: {table}/{identifier}')
    fields = dict(op.get('set', {}))
    if not fields.keys() <= EDITABLE[table]:
        raise ValueError('Unknown/protected field in patch')
    if 'confirmation' in op and table != 'record':
        raise ValueError('Only records accept confirmation reviews')
    if table == 'record':
        assess(db, op, before, fields)
    if table != 'record' and ('evidence' in op or 'participants' in op):
        raise ValueError('Only records accept evidence/participants')
    if table == 'record' and before and 'review_status' not in fields and 'confirmation' not in op:
        fields['review_status'] = 'draft'
    if before:
        fields['version'] = before['version'] + 1
        assignments = ','.join(f'{key}=?' for key in fields)
        db.execute(f'UPDATE {table} SET {assignments} WHERE id=?', (*fields.values(), identifier))
    else:
        insert(db, table, dict(id=identifier, **fields))
    if table == 'record':
        if 'evidence' in op:
            db.execute('DELETE FROM evidence WHERE record_id=?', (identifier,))
            for source in op['evidence']:
                insert(db, 'evidence', dict(record_id=identifier, passage_id=get_passage(db, source),
                    kind=source['kind'], speaker_id=source.get('speaker_id'), note=source.get('note', '')))
        if 'participants' in op:
            db.execute('DELETE FROM record_entity WHERE record_id=?', (identifier,))
            for participant in op['participants']:
                insert(db, 'record_entity', dict(record_id=identifier, **participant))
        if not db.execute('SELECT 1 FROM evidence WHERE record_id=?', (identifier,)).fetchone():
            raise ValueError(f'Record {identifier} requires evidence, including a question trigger')
    if table == 'article' and ('body' in fields or before is None):
        body = db.execute('SELECT body FROM article WHERE id=?', (identifier,)).fetchone()[0]
        refs = set(re.findall(r'\[\[([A-Za-z0-9_-]+)\]\]', body))
        if not refs:
            raise ValueError('Article needs [[record-id]] evidence markers')
        db.execute('DELETE FROM article_record WHERE article_id=?', (identifier,))
        for record_id in sorted(refs):
            row = db.execute('SELECT version FROM record WHERE id=?', (record_id,)).fetchone()
            if row is None:
                raise ValueError(f'Unknown article evidence: {record_id}')
            insert(db, 'article_record', dict(article_id=identifier, record_id=record_id, used_version=row[0]))
    after = snapshot(db, table, identifier)
    insert(db, 'revision', dict(target_table=table, target_id=identifier,
           before_json=dumps(before) if before else None, after_json=dumps(after), reason=reason, editor=editor))


def apply_batch(db, batch):
    if not batch.get('reason', '').strip() or not batch.get('editor', '').strip():
        raise ValueError('A reason and editor are required')
    with db:
        db.execute('BEGIN IMMEDIATE')
        for op in batch['operations']:
            apply_operation(db, op, batch['reason'], batch['editor'])


def create_database(path):
    path = Path(path)
    if path.exists():
        raise ValueError('Database already exists; initialization never overwrites edits')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=False)
    db = connect(path)
    try:
        db.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        migrate(db)
    except Exception:
        db.close()
        path.unlink()  # Only the new failed DB; existing files were refused above.
        raise
    db.close()


def validate(db):
    issues = []
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        issues.append('SQLite integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        issues.append('Foreign key check failed')
    for row in db.execute('''SELECT p.*, c.raw_text FROM passage p JOIN chapter c ON c.id=p.chapter_id'''):
        if row['quote'] != row['raw_text'][row['start_char']:row['end_char']]:
            issues.append('Evidence mismatch: ' + row['id'])
    for row in db.execute('SELECT id FROM record WHERE id NOT IN (SELECT record_id FROM evidence)'):
        issues.append('Missing evidence: ' + row[0])
    for a in db.execute('SELECT id,body FROM article'):
        markers = set(re.findall(r'\[\[([A-Za-z0-9_-]+)\]\]', a['body']))
        bound = {r[0] for r in db.execute('SELECT record_id FROM article_record WHERE article_id=?', (a['id'],))}
        if markers != bound:
            issues.append('Article evidence binding mismatch: ' + a['id'])
    return issues


def find_records(db, query):
    # Literal substring search preserves one- and two-character Chinese terms.
    return [dict(r) for r in db.execute('''SELECT DISTINCT r.* FROM record r
        LEFT JOIN record_entity re ON re.record_id=r.id
        WHERE instr(r.title,?)>0 OR instr(r.content,?)>0
        OR r.subject_id IN (SELECT entity_id FROM entity_names WHERE instr(name,?)>0)
        OR r.object_id IN (SELECT entity_id FROM entity_names WHERE instr(name,?)>0)
        OR re.entity_id IN (SELECT entity_id FROM entity_names WHERE instr(name,?)>0)
        ORDER BY r.id''', (query,) * 5)]


def export(db, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    records = [snapshot(db, 'record', r[0]) for r in db.execute('SELECT id FROM record ORDER BY id')]
    from progress import build_status
    status = build_status(db)
    lines = ['# 原作资料提取记录', '',
        '范围以当前数据库中的来源与章节为准。记录附带编辑和采用状态；成功入库不代表语义审核完成。', '',
        '段落号从 p000 开始；仅当第一段与章节标题一致时，正文从 p001 开始。', '']
    for scope in status['sources']:
        total = scope['total_chapters'] if scope['total_chapters'] is not None else '未知'
        lines.extend([f'- {scope["title"]}：来源目录 {total} 章，已入库 {scope["imported_chapters"]} 章，'
            f'登记已读 {scope["read_chapters"]} 章，排除 {scope["excluded_chapters"]} 章。'])
    lines.append('')
    for chapter in db.execute('SELECT * FROM chapter ORDER BY document_id,source_sequence'):
        lines.extend(['## ' + chapter['title'], '',
            f'来源：{chapter["document_id"]}；目录序号：{chapter["source_sequence"]}；'
            f'阅读：{chapter["reading_status"]}；提取：{chapter["extraction_status"]}。', '',
            chapter['coverage_note'], ''])
        chapter_records = [r for r in records if any(e['chapter_id'] == chapter['id'] for e in r['evidence'])]
        for r in chapter_records:
            lines.extend([f'<a id="{r["id"]}"></a>', '', f'### {r["id"]} · {r["title"]}', '', r['content'], '',
                f'类型：{r["kind"]}；依据：{LABELS[r["source_status"]]}；状态：{LABELS[r["modality"]]}；'
                f'编辑：{r["review_status"]}。', ''])
            if r['story_time']:
                lines.extend(['故事时间：' + r['story_time'], ''])
            if r['scope_note']:
                lines.extend(['整理说明：' + r['scope_note'], ''])
            confirmation_labels = {'judgment':'判断', 'provisional':'初步确定',
                                   'disputed':'存在冲突', 'rejected':'已撤回'}
            if r['confirmation_status'] in confirmation_labels:
                lines.extend(['确认标签：' + confirmation_labels[r['confirmation_status']], ''])
            refs = [f'{e["chapter_id"]} p{e["paragraph_start"]:03}—p{e["paragraph_end"]:03}' for e in r['evidence']]
            lines.extend(['原文依据：' + '；'.join(refs) + '。完整片段保存在数据库，可用 show 查看。', ''])
    lines.extend(['## 跨记录联系', ''])
    for link in db.execute('SELECT * FROM record_link ORDER BY from_id,to_id'):
        lines.extend([f'- {link["from_id"]} → {link["to_id"]}（{link["relation"]}）：{link["note"]}'])
    (directory / '提取记录.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    for a in db.execute('SELECT a.*,s.changed_records FROM article a JOIN article_staleness s ON s.id=a.id'):
        body = re.sub(r'\[\[([A-Za-z0-9_-]+)\]\]', r'[\1](提取记录.md#\1)', a['body'])
        warning = f'\n\n> 有 {a["changed_records"]} 条依据已修改，需要重新核对正文。' if a['changed_records'] else ''
        (directory / f'{a["id"]}.md').write_text(
            f'# {a["title"]}\n\n{a["scope_note"]}{warning}\n\n{body}\n', encoding='utf-8')
    # Reading/export artifact, not a backup: full chapter text and evidence quotations stay local in SQLite.
    clean_records = [{**r, 'evidence': [{k: v for k, v in e.items() if k != 'quote'} for e in r['evidence']]}
                     for r in records]
    payload = dict(schema_version=2, purpose='review/export; not a database backup',
        source_scope=status['sources'],
        entities=[dict(r) for r in db.execute('SELECT * FROM entity ORDER BY id')], records=clean_records,
        links=[dict(r) for r in db.execute('SELECT * FROM record_link')],
        articles=[snapshot(db, 'article', r[0]) for r in db.execute('SELECT id FROM article')],
        article_staleness=[dict(r) for r in db.execute('SELECT * FROM article_staleness')])
    (directory / 'review-data.json').write_text(dumps(payload) + '\n', encoding='utf-8')
    (directory / 'progress.json').write_text(dumps(status) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('create', help='Create an empty database; file reading is handled by the agent')
    show = sub.add_parser('show')
    show.add_argument('table', choices=EDITABLE)
    show.add_argument('id')
    search = sub.add_parser('search')
    search.add_argument('query')
    apply = sub.add_parser('apply')
    apply.add_argument('file', type=Path)
    history = sub.add_parser('history')
    history.add_argument('table', choices=EDITABLE)
    history.add_argument('id')
    out = sub.add_parser('export')
    out.add_argument('--out', type=Path, required=True)
    backup = sub.add_parser('backup')
    backup.add_argument('--out', type=Path, required=True)
    packs = sub.add_parser('packs')
    packs.add_argument('--out', type=Path, required=True)
    packs.add_argument('--max-chars', type=int, default=2200)
    ingest_parser = sub.add_parser('ingest')
    ingest_parser.add_argument('--pack', type=Path, required=True)
    ingest_parser.add_argument('--result', type=Path, required=True)
    sub.add_parser('check')
    sub.add_parser('migrate')
    status_parser = sub.add_parser('status', help='Read-only progress and bounded work queues')
    status_parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if args.command == 'create':
        create_database(args.db)
        print('Initialized:', args.db)
        return
    db = connect(args.db, readonly=args.command not in ('apply', 'ingest', 'migrate'))
    try:
        if args.command == 'status':
            from progress import build_status
            print(dumps(build_status(db, args.limit)))
        elif args.command == 'migrate':
            migrate(db)
            print('Database schema: 2; provenance and existing edits preserved.')
        elif args.command == 'show':
            value = snapshot(db, args.table, args.id)
            if value is None:
                raise ValueError('Object not found')
            print(dumps(value))
        elif args.command == 'search':
            print(dumps(find_records(db, args.query)))
        elif args.command == 'apply':
            apply_batch(db, json.loads(args.file.read_text(encoding='utf-8-sig')))
            print('Applied transaction; export again to refresh review files.')
        elif args.command == 'history':
            print(dumps([dict(r) for r in db.execute(
                'SELECT * FROM revision WHERE target_table=? AND target_id=? ORDER BY id', (args.table, args.id))]))
        elif args.command == 'export':
            export(db, args.out)
            print('Exported:', args.out)
        elif args.command == 'packs':
            from reading_packs import write_packs
            print(dumps(write_packs(db, args.out, args.max_chars)))
        elif args.command == 'ingest':
            from reading_packs import ingest
            print(ingest(db, args.pack, args.result))
        elif args.command == 'backup':
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.touch(exist_ok=False)
            target = sqlite3.connect(args.out)
            try:
                db.backup(target)
            finally:
                target.close()
            print('Backed up:', args.out)
        elif args.command == 'check':
            issues = validate(db)
            print(dumps(dict(issues=issues, articles=[dict(r) for r in db.execute('SELECT * FROM article_staleness')],
                counts={t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                        for t in ('chapter','entity','record','evidence','article','revision')})))
            if issues:
                raise SystemExit(1)
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, sqlite3.Error, OSError) as error:
        raise SystemExit(str(error))
