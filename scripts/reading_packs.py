"""Bounded reading tasks and a small model-facing JSON format."""
import hashlib
import json
import re
from pathlib import Path

from wiki import paragraphs, body_start, dumps, apply_operation, insert

KINDS = {'detail': 'fact', 'event': 'event', 'relation': 'relation', 'question': 'question'}
BASES = {'narration': ('explicit', 'narration'), 'dialogue': ('attributed', 'dialogue'),
         'thought': ('attributed', 'thought'), 'inference': ('interpretation', 'mixed'),
         'question': ('unresolved', 'mixed')}
STATES = {'actual', 'planned', 'general', 'unknown'}


def write_packs(db, out, max_chars=2200):
    if max_chars < 500:
        raise ValueError('Use a character budget of at least 500')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    names = [dict(r) for r in db.execute('SELECT entity_id,name,kind FROM entity_names')]
    for chapter in db.execute("SELECT * FROM chapter WHERE reading_status!='excluded' ORDER BY document_id,source_sequence"):
        matches = paragraphs(chapter['raw_text'])
        first_body = body_start(chapter)
        start = first_body
        while start < len(matches):
            end, used = start, 0
            while end < len(matches):
                size = len(matches[end].group())
                if end > start and used + size > max_chars:
                    break
                used += size
                end += 1
            # One preceding paragraph is context only, not additional assigned work.
            context_start = max(first_body, start - 1)
            paragraph_rows = [dict(number=i, text=matches[i].group(), assigned=i >= start)
                              for i in range(context_start, end)]
            combined = ''.join(p['text'] for p in paragraph_rows)
            pack = dict(id=f'{chapter["id"]}-p{start:03}-{end-1:03}', chapter_id=chapter['id'],
                chapter_title=chapter['title'], source_sequence=chapter['source_sequence'],
                chapter_sha256=hashlib.sha256(chapter['raw_text'].encode('utf-8')).hexdigest(),
                assigned_start=start, assigned_end=end-1, paragraphs=paragraph_rows,
                known_names=[n for n in names if n['name'] in combined],
                note='只提取本包负责段落；前一段仅提供衔接。广告不作作品资料。'
                     '称呼表只是提示；不得用未提供的全书知识补写。单段超预算时保持完整。')
            (out / f'{pack["id"]}.json').write_text(dumps(pack) + '\n', encoding='utf-8')
            index.append(dict(id=pack['id'], chapter_id=chapter['id'],
                assigned_start=start, assigned_end=end-1, assigned_chars=used,
                input_chars=len(dumps(pack))))
            start = end
    (out / 'index.json').write_text(dumps(index) + '\n', encoding='utf-8')
    return index


def resolve_name(db, name):
    if name is None or name == '':
        return None
    if not isinstance(name, str):
        raise ValueError('Names must be strings')
    # Only unique canonical names receive draft links. Contextual aliases wait for review.
    rows = {r[0] for r in db.execute('SELECT id FROM entity WHERE name=?', (name,))}
    return next(iter(rows)) if len(rows) == 1 else None


def compile_submission(db, pack, result):
    chapter = db.execute('SELECT * FROM chapter WHERE id=?', (pack['chapter_id'],)).fetchone()
    if chapter is None or hashlib.sha256(chapter['raw_text'].encode('utf-8')).hexdigest() != pack['chapter_sha256']:
        raise ValueError('Pack source does not match the database edition')
    lines = paragraphs(chapter['raw_text'])
    first_body = body_start(chapter)
    if chapter['reading_status'] == 'excluded':
        raise ValueError('Excluded chapters are not extraction sources')
    start, end = pack['assigned_start'], pack['assigned_end']
    if type(start) is not int or type(end) is not int or not first_body <= start <= end < len(lines):
        raise ValueError('Invalid assigned paragraph range')
    expected_id = f'{chapter["id"]}-p{start:03}-{end:03}'
    if pack['id'] != expected_id or result.get('pack_id') != expected_id:
        raise ValueError('Wrong reading pack')
    task = result.get('task')
    if task not in ('events', 'details'):
        raise ValueError('task must be events or details')
    expected_numbers = list(range(max(first_body, start-1), end+1))
    if [p['number'] for p in pack['paragraphs']] != expected_numbers:
        raise ValueError('Pack paragraphs are incomplete or out of order')
    for paragraph in pack['paragraphs']:
        if paragraph['text'] != lines[paragraph['number']].group():
            raise ValueError('Reading-pack text differs from original source')
        if paragraph.get('assigned') is not (paragraph['number'] >= start):
            raise ValueError('Reading-pack assignment flags are inconsistent')
    items = result.get('items')
    if not isinstance(items, list) or not 0 <= len(items) <= 40:
        raise ValueError('Submit 0–40 items per batch')
    operations = []
    for i, item in enumerate(items, 1):
        for field in ('title', 'content', 'subject', 'kind', 'basis', 'state'):
            if not isinstance(item.get(field), str) or (field != 'subject' and not item[field].strip()):
                raise ValueError(f'Item {i}: missing/non-text {field}')
        if item['kind'] not in KINDS or item['basis'] not in BASES or item['state'] not in STATES:
            raise ValueError(f'Item {i}: invalid kind/basis/state')
        if (task == 'events' and item['kind'] not in ('event', 'question')) or (task == 'details' and item['kind'] == 'event'):
            raise ValueError('Event and detail passes must be submitted separately')
        if (item['kind'] == 'question') != (item['basis'] == 'question'):
            raise ValueError('Questions need both kind=question and basis=question')
        bounds = item.get('paragraphs')
        if not isinstance(bounds, list) or len(bounds) != 2 or not all(type(n) is int for n in bounds):
            raise ValueError('paragraphs must be [first, last]')
        first, last = bounds
        if not max(first_body, start-1) <= first <= last <= end or last < start:
            raise ValueError('Evidence must stay inside the pack and touch assigned paragraphs')
        subject_id = resolve_name(db, item['subject'])
        speaker_id = resolve_name(db, item.get('speaker'))
        object_id = resolve_name(db, item.get('object'))
        status, evidence_kind = BASES[item['basis']]
        operations.append(dict(table='record', id=f'D-{expected_id}-{task}-{i:02}', expected_version=None,
            set=dict(kind=KINDS[item['kind']], title=item['title'], content=item['content'],
                subject_id=subject_id, object_id=object_id, predicate=item.get('predicate', ''),
                source_status=status, modality=item['state'], story_time=item.get('story_time', ''),
                scope_note=f'分段模型草稿；来源包 {expected_id}；原样称呼：{item["subject"]}。'
                    + ('称呼未唯一解析，需归并。' if item['subject'] and not subject_id else '')
                    + ('关系对象：' + item['object'] if item.get('object') else ''),
                review_status='draft'), evidence=[dict(chapter_id=chapter['id'],
                    paragraph_start=first, paragraph_end=last, kind=evidence_kind, speaker_id=speaker_id,
                    note='模型给出的说话人：' + str(item.get('speaker', '未标明')))]))
    return dict(reason='导入分段模型输出，仅通过结构和证据位置校验，尚未语义审阅',
                editor='model-draft-import', operations=operations)


def ingest(db, pack_path, result_path):
    pack = json.loads(Path(pack_path).read_text(encoding='utf-8-sig'))
    result = json.loads(Path(result_path).read_text(encoding='utf-8-sig'))
    batch = compile_submission(db, pack, result)
    digest = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    with db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT result_sha256 FROM reading_task WHERE pack_id=? AND task=?',
                              (pack['id'], result['task'])).fetchone()
        if previous:
            if previous[0] == digest:
                return 'already imported; existing edits preserved'
            raise ValueError('This task already has a different result; use versioned apply to correct it')
        for operation in batch['operations']:
            apply_operation(db, operation, batch['reason'], batch['editor'])
        insert(db, 'reading_task', dict(pack_id=pack['id'], task=result['task'], chapter_id=pack['chapter_id'],
            assigned_start=pack['assigned_start'], assigned_end=pack['assigned_end'], result_sha256=digest,
            record_count=len(batch['operations'])))
    return 'imported draft records; semantic review is still required'
