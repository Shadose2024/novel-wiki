"""Run the storage workflow with original synthetic text and prewritten records."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wiki
import reading_packs

CHAPTERS = [
    '第一章 图纸\n阿青是河湾村的木匠。她准备修桥，估计需要约十天。\n阿青说：先勘测河床，再决定桥墩的位置。\n',
    '第二章 通行\n十二天后，木桥完工并开放通行。阿青把图纸交给学徒小禾。\n阿青解释：雨水使施工比最初预计晚了两天。\n',
]


def run(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    text = ''.join(CHAPTERS)
    source = out / 'source.txt'
    source.write_bytes(text.encode('utf-8'))
    wiki.create_database(out / 'wiki.sqlite')
    db = wiki.connect(out / 'wiki.sqlite')
    try:
        with db:
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            wiki.insert(db, 'source_document', dict(id='demo', title='河湾木桥（合成示例）',
                original_path=str(source.resolve()), source_format='txt', encoding='utf-8',
                raw_sha256=digest, decoded_sha256=digest,
                locator_policy='UTF-8 snapshot; Unicode code points, zero-based [start,end); chapter offsets global, evidence local.'))
            wiki.insert(db, 'source_scope', dict(document_id='demo', total_chapters=2, note='此示例仅有两章。'))
            start = 0
            for n, chapter in enumerate(CHAPTERS, 1):
                wiki.insert(db, 'chapter', dict(id=f'c{n}', document_id='demo', source_sequence=n,
                    title=chapter.splitlines()[0], start_char=start, end_char=start+len(chapter),
                    raw_text=chapter, reading_status='read', extraction_status='draft',
                    coverage_note='合成演示的预设状态；没有在此调用模型阅读。'))
                start += len(chapter)
        wiki.apply_batch(db, dict(reason='初始化合成示例对象', editor='demo', operations=[
            dict(table='entity', id=key, expected_version=None, set=dict(name=name, kind=kind))
            for key, name, kind in [('qing','阿青','character'), ('he','小禾','character'), ('bridge','木桥','structure')]]))
        index = reading_packs.write_packs(db, out/'packs')
        preset = {
            ('c1','events'): [dict(kind='event', title='阿青的修桥计划', content='阿青准备修桥，估计需要约十天。',
                subject='阿青', basis='narration', state='planned', paragraphs=[1,1])],
            ('c1','details'): [dict(kind='detail', title='阿青的职业', content='阿青是河湾村的木匠。',
                subject='阿青', basis='narration', state='general', paragraphs=[1,1]),
                dict(kind='detail', title='确定桥墩前的勘测', content='阿青提出先勘测河床，再决定桥墩位置。',
                subject='阿青', basis='dialogue', speaker='阿青', state='planned', paragraphs=[2,2])],
            ('c2','events'): [dict(kind='event', title='木桥开放通行', content='十二天后，木桥完工并开放通行。',
                subject='木桥', basis='narration', state='actual', paragraphs=[1,1], story_time='十二天后')],
            ('c2','details'): [dict(kind='relation', title='小禾是阿青的学徒', content='小禾是阿青的学徒，收到她交来的图纸。',
                subject='小禾', predicate='师父', object='阿青', basis='narration', state='actual', paragraphs=[1,1]),
                dict(kind='detail', title='阿青说明施工延误原因', content='阿青说，雨水使施工比最初预计晚了两天。',
                subject='阿青', basis='dialogue', speaker='阿青', state='actual', paragraphs=[2,2])],
        }
        for pack in index:
            for task in ('events','details'):
                result = dict(pack_id=pack['id'], task=task, items=preset[(pack['chapter_id'],task)])
                path = out / f'{pack["id"]}-{task}.json'
                path.write_text(wiki.dumps(result), encoding='utf-8')
                reading_packs.ingest(db, out/'packs'/f'{pack["id"]}.json', path)
        body = ('阿青是河湾村的木匠。[[D-c1-p001-002-details-01]]\n\n'
            '## 人物档案\n\n| 项目 | 资料 |\n|---|---|\n'
            '| 职业 | 河湾村木匠。[[D-c1-p001-002-details-01]] |\n'
            '| 学徒 | 小禾。[[D-c2-p001-002-details-01]] |\n\n'
            '## 修桥经历\n\n阿青计划修桥，最初估计需要约十天。'
            '她提出先勘测河床，再确定桥墩位置。[[D-c1-p001-002-events-01]][[D-c1-p001-002-details-02]]\n\n'
            '十二天后，木桥完工并开放通行，阿青将图纸交给小禾。'
            '她把比预计晚两天归因于雨水。[[D-c2-p001-002-events-01]][[D-c2-p001-002-details-01]][[D-c2-p001-002-details-02]]')
        wiki.apply_batch(db, dict(reason='演示草稿如何引用资料及版本', editor='demo', operations=[
            dict(table='article', id='qing', expected_version=None, set=dict(entity_id='qing', title='阿青',
                body=body, scope_note='合成短文的预写示例；资料保留draft和judgment，非模型提取成绩。'))]))
        issues = wiki.validate(db)
        if issues:
            raise ValueError('; '.join(issues))
        wiki.export(db, out/'review')
    finally:
        db.close()
    return out/'review'/'qing.md'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='A new output directory')
    try:
        print(run(parser.parse_args().out))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
