"""Read-only work queues. Completion of a task never implies semantic correctness."""
from wiki import paragraphs, body_start


def missing_ranges(first, last, intervals):
    missing, cursor = [], first
    for start, end in sorted(intervals):
        if end < cursor or start > last:
            continue
        if start > cursor:
            missing.append([cursor, start - 1])
        cursor = max(cursor, end + 1)
    if cursor <= last:
        missing.append([cursor, last])
    return missing


def build_status(db, limit=20):
    if type(limit) is not int or limit < 1:
        raise ValueError('limit must be a positive integer')
    sources = []
    for doc in db.execute('''SELECT d.id,d.title,s.total_chapters FROM source_document d
            LEFT JOIN source_scope s ON s.document_id=d.id ORDER BY d.id'''):
        chapters = db.execute('SELECT * FROM chapter WHERE document_id=? ORDER BY source_sequence',
                              (doc['id'],)).fetchall()
        total = doc['total_chapters']
        seqs = [c['source_sequence'] for c in chapters]
        gaps = missing_ranges(1, total, [(n, n) for n in seqs]) if total is not None else None
        complete = bool(total and len(seqs) == total and not gaps and all(1 <= n <= total for n in seqs))
        sources.append(dict(document_id=doc['id'], title=doc['title'], total_chapters=total,
            imported_chapters=len(chapters), read_chapters=sum(c['reading_status']=='read' for c in chapters),
            excluded_chapters=sum(c['reading_status']=='excluded' for c in chapters),
            missing_chapter_ranges=gaps,
            invalid_sequence_count=sum(n < 1 or (total is not None and n > total) for n in seqs),
            source_read_complete=complete and all(c['reading_status']=='read' or
                (c['reading_status']=='excluded' and bool(c['coverage_note'].strip())) for c in chapters)))

    # Existing task intervals are used instead of assuming the original pack size.
    pending, invalid = [], []
    for chapter in db.execute("SELECT * FROM chapter WHERE reading_status!='excluded' ORDER BY document_id,source_sequence"):
        first, last = body_start(chapter), len(paragraphs(chapter['raw_text'])) - 1
        for task in ('events', 'details'):
            spans = []
            for row in db.execute('SELECT * FROM reading_task WHERE chapter_id=? AND task=?', (chapter['id'], task)):
                if not first <= row['assigned_start'] <= row['assigned_end'] <= last:
                    invalid.append(dict(pack_id=row['pack_id'], task=task, chapter_id=chapter['id']))
                else:
                    spans.append((row['assigned_start'], row['assigned_end']))
            missing = missing_ranges(first, last, spans)
            if missing:
                pending.append(dict(chapter_id=chapter['id'], task=task, missing_paragraph_ranges=missing))

    def queue(sql):
        total = db.execute('SELECT count(*) FROM (' + sql + ')').fetchone()[0]
        return dict(total=total, items=[dict(r) for r in db.execute(sql + ' LIMIT ?', (limit,))],
                    truncated=total > limit)

    def bounded(items):
        return dict(total=len(items), items=items[:limit], truncated=len(items) > limit)

    return dict(schema_version=db.execute('PRAGMA user_version').fetchone()[0], sources=sources,
        counts={t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                for t in ('chapter','entity','record','reading_task','article')},
        record_states={field: {r[0]: r[1] for r in db.execute(f'SELECT {field},count(*) FROM record GROUP BY {field}')}
                       for field in ('review_status', 'confirmation_status')},
        queues=dict(
            unread_chapters=queue("SELECT id,title FROM chapter WHERE reading_status='unread' ORDER BY document_id,source_sequence"),
            exclusions_without_reason=queue("SELECT id,title FROM chapter WHERE reading_status='excluded' AND trim(coverage_note)='' ORDER BY id"),
            extraction_gaps=bounded(pending), invalid_task_ranges=bounded(invalid),
            draft_records=queue("SELECT id,title FROM record WHERE review_status='draft' AND confirmation_status!='rejected' ORDER BY id"),
            unlinked_subjects=queue("SELECT id,title,scope_note FROM record WHERE subject_id IS NULL AND confirmation_status!='rejected' ORDER BY id"),
            open_questions=queue("SELECT id,title FROM record WHERE (kind='question' OR source_status='unresolved' OR confirmation_status='disputed') AND confirmation_status!='rejected' ORDER BY id"),
            provisional_records=queue("SELECT id,title,confirmed_through FROM record WHERE confirmation_status='provisional' ORDER BY id"),
            stale_articles=queue('SELECT * FROM article_staleness WHERE changed_records>0 ORDER BY id')),
        notes=['source_read_complete仅表示登记的来源范围已处理，不代表原版本无缺文或百科提取已完成。',
               'extraction_gaps表示尚无分包提交覆盖的段落；已有人工整理时先核对，不自动重做。',
               '队列只安排复核；无主体记录可能合理，需由模型判断。本文不修改任何状态。'])
