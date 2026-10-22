"""Portable skill regression tests using synthetic source text only."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import wiki
import reading_packs
from progress import build_status


class SkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'work.sqlite'
        wiki.create_database(self.path)
        self.db = wiki.connect(self.path)
        raw = '第一章\n阿青提出建桥计划。\n桥必须先完成勘测。\n'
        with self.db:
            wiki.insert(self.db, 'source_document', dict(id='book', title='虚构测试书', original_path='source.txt',
                source_format='txt', encoding='utf-8', raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                decoded_sha256=hashlib.sha256(raw.encode()).hexdigest(), locator_policy='test snapshot'))
            wiki.insert(self.db, 'source_scope', dict(document_id='book', total_chapters=12))
            self.chapter(1, raw)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def chapter(self, n, text, state='unread', note=''):
        wiki.insert(self.db, 'chapter', dict(id=f'c{n}', document_id='book', source_sequence=n,
            title='第一章' if n == 1 else f'第{n}章', start_char=n*100, end_char=n*100+len(text),
            raw_text=text, reading_status=state, extraction_status='pending', coverage_note=note))

    def apply(self, operations):
        wiki.apply_batch(self.db, dict(reason='synthetic test', editor='test', operations=operations))

    def submission(self):
        reading_packs.write_packs(self.db, self.root/'packs')
        packfile = next((self.root/'packs').glob('c1-*.json'))
        pack = json.loads(packfile.read_text(encoding='utf-8'))
        result = dict(pack_id=pack['id'], task='events', items=[dict(kind='event', title='建桥计划',
            content='阿青提出建桥计划。', subject='阿青', basis='narration', state='planned', paragraphs=[1,1])])
        resultfile = self.root/'result.json'
        resultfile.write_text(wiki.dumps(result), encoding='utf-8')
        return pack, result, packfile, resultfile

    def test_moved_package_has_no_project_dependency(self):
        moved = self.root/'moved-skill'
        shutil.copytree(SKILL, moved, ignore=shutil.ignore_patterns('__pycache__', '.git', 'dist', 'demo-output'))
        fresh = self.root/'fresh.sqlite'
        base = [sys.executable, '-X', 'utf8', str(moved/'scripts/wiki.py'), '--db', str(fresh)]
        subprocess.run(base+['create'], check=True, capture_output=True, cwd=self.root)
        result = subprocess.run(base+['status'], check=True, capture_output=True, text=True, encoding='utf-8', cwd=self.root)
        self.assertEqual(json.loads(result.stdout)['counts']['record'], 0)
        self.assertNotEqual(subprocess.run(base+['create'], capture_output=True, cwd=self.root).returncode, 0)

    def test_no_heading_keeps_first_paragraph_and_excluded_is_skipped(self):
        with self.db:
            self.chapter(2, '第一段就是正文。\n第二段补充限制。')
            self.chapter(3, '非作品广告', 'excluded', '广告')
        index = reading_packs.write_packs(self.db, self.root/'packs')
        self.assertFalse(any(p['chapter_id']=='c3' for p in index))
        row = next(p for p in index if p['chapter_id']=='c2')
        self.assertEqual(row['assigned_start'], 0)
        pack = json.loads((self.root/'packs'/f'{row["id"]}.json').read_text(encoding='utf-8'))
        result = dict(pack_id=pack['id'], task='details', items=[dict(kind='detail', title='正文',
            content='第一段就是正文。', subject='', basis='narration', state='general', paragraphs=[0,0])])
        op = reading_packs.compile_submission(self.db, pack, result)['operations'][0]
        self.assertEqual(op['evidence'][0]['paragraph_start'], 0)

    def test_changed_pack_flags_and_text_are_rejected(self):
        pack, result, _, _ = self.submission()
        wrong = copy.deepcopy(pack)
        wrong['paragraphs'][0]['assigned'] = False
        with self.assertRaises(ValueError):
            reading_packs.compile_submission(self.db, wrong, result)
        wrong = copy.deepcopy(pack)
        wrong['paragraphs'][0]['text'] = '替换原文'
        with self.assertRaises(ValueError):
            reading_packs.compile_submission(self.db, wrong, result)

    def test_empty_task_and_partial_submission_do_not_imply_review(self):
        pack, result, pf, rf = self.submission()
        result['items'] = []
        rf.write_text(wiki.dumps(result), encoding='utf-8')
        reading_packs.ingest(self.db, pf, rf)
        status = build_status(self.db)
        gaps = status['queues']['extraction_gaps']['items']
        self.assertEqual([g['task'] for g in gaps], ['details'])
        self.assertEqual(self.db.execute('SELECT reading_status FROM chapter').fetchone()[0], 'unread')
        self.assertFalse(status['sources'][0]['source_read_complete'])

    def test_retry_preserves_edits_and_stale_patch_rolls_back(self):
        pack, result, pf, rf = self.submission()
        reading_packs.ingest(self.db, pf, rf)
        rid = f'D-{pack["id"]}-events-01'
        change = dict(table='record', id=rid, expected_version=1, set=dict(scope_note='补充说明'))
        self.apply([change])
        reading_packs.ingest(self.db, pf, rf)
        self.assertEqual(wiki.snapshot(self.db, 'record', rid)['scope_note'], '补充说明')
        with self.assertRaises(ValueError):
            self.apply([dict(table='entity', id='temporary', expected_version=None, set=dict(name='临时对象',kind='person')), change])
        self.assertIsNone(wiki.snapshot(self.db, 'entity', 'temporary'))
        self.assertEqual(wiki.validate(self.db), [])

    def test_full_source_requires_known_scope_no_gaps_and_exclusion_reasons(self):
        with self.db:
            self.db.execute("UPDATE chapter SET reading_status='read'")
            self.chapter(12, '末章正文。', 'read')
        status = build_status(self.db)
        self.assertEqual(status['sources'][0]['missing_chapter_ranges'], [[2,11]])
        self.assertFalse(status['sources'][0]['source_read_complete'])
        with self.db:
            for n in range(2,12):
                self.chapter(n, '附加正文。', 'read')
            self.db.execute("UPDATE chapter SET reading_status='excluded' WHERE id='c2'")
        self.assertFalse(build_status(self.db)['sources'][0]['source_read_complete'])
        with self.db:
            self.db.execute("UPDATE chapter SET coverage_note='已核实为重复拼接' WHERE id='c2'")
        self.assertTrue(build_status(self.db)['sources'][0]['source_read_complete'])
        with self.db:
            self.db.execute('UPDATE source_scope SET total_chapters=NULL')
        self.assertFalse(build_status(self.db)['sources'][0]['source_read_complete'])

    def test_confirmation_preserves_planned_state_and_edits_reopen(self):
        pack, result, pf, rf = self.submission()
        reading_packs.ingest(self.db, pf, rf)
        rid = f'D-{pack["id"]}-events-01'
        with self.db:
            self.db.execute("UPDATE chapter SET reading_status='read'")
            for n in range(2,13):
                self.chapter(n, '后文没有否定该计划的提出。', 'read')
        self.apply([dict(table='record', id=rid, expected_version=1, set=dict(review_status='agent_checked'))])
        review = dict(status='provisional', basis='window', through_chapter='c11', note='测试后续复核',
            faithful_to_source=True, no_unresolved_conflict=True, eligible=True)
        self.apply([dict(table='record', id=rid, expected_version=2, confirmation=review)])
        review.update(status='fact', basis='full_text', through_chapter='c12')
        self.apply([dict(table='record', id=rid, expected_version=3, confirmation=review)])
        rec = wiki.snapshot(self.db, 'record', rid)
        self.assertEqual((rec['confirmation_status'],rec['modality']), ('fact','planned'))
        self.apply([dict(table='record', id=rid, expected_version=4, set=dict(content='修订内容'))])
        self.assertEqual(wiki.snapshot(self.db, 'record', rid)['confirmation_status'], 'judgment')

    def test_export_uses_actual_scope_and_reports_stale_article(self):
        pack, result, pf, rf = self.submission()
        reading_packs.ingest(self.db, pf, rf)
        rid = f'D-{pack["id"]}-events-01'
        self.apply([
            dict(table='entity', id='aq', expected_version=None, set=dict(name='阿青',kind='character')),
            dict(table='article', id='aq-page', expected_version=None, set=dict(entity_id='aq',title='阿青',
                body=f'阿青提出建桥计划。[[{rid}]]',scope_note='测试样本'))])
        self.apply([dict(table='record', id=rid, expected_version=1, set=dict(scope_note='已修改'))])
        before = self.db.total_changes
        status = build_status(self.db, 1)
        self.assertEqual(status['queues']['stale_articles']['total'], 1)
        self.assertEqual(self.db.total_changes, before)
        wiki.export(self.db, self.root/'review')
        data = json.loads((self.root/'review/review-data.json').read_text(encoding='utf-8'))
        self.assertEqual(data['source_scope'][0]['imported_chapters'], 1)
        self.assertNotIn('raw_text', json.dumps(data))
        self.assertTrue((self.root/'review/progress.json').is_file())
        self.assertEqual(wiki.validate(self.db), [])

    def test_documented_demo_preserves_plan_and_later_completion(self):
        output = self.root/'demo'
        subprocess.run([sys.executable, '-X', 'utf8', str(SKILL/'examples/demo.py'), '--out', str(output)],
                       check=True, capture_output=True, cwd=self.root)
        data = json.loads((output/'review/review-data.json').read_text(encoding='utf-8'))
        records = {r['id']:r for r in data['records']}
        self.assertEqual(records['D-c1-p001-002-events-01']['modality'], 'planned')
        self.assertEqual(records['D-c2-p001-002-events-01']['modality'], 'actual')
        self.assertEqual(data['article_staleness'][0]['changed_records'], 0)
        self.assertTrue((output/'review/qing.md').is_file())
        second = subprocess.run([sys.executable, str(SKILL/'examples/demo.py'), '--out', str(output)], capture_output=True)
        self.assertNotEqual(second.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
