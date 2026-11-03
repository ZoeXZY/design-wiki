#!/usr/bin/env python3
"""Exercise the Skill CLI against an isolated local server; no real API calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT/'assets/runtime'))
from server import Workspace, make_server


class SkillCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='design wiki isolated ')
        self.w = Workspace(Path(self.tmp.name)/'workspace.json')
        self.calls = []
        def fake(messages):
            self.calls.append(messages)
            answer = '明确标注的脚本测试回答，非真实API。[E1]'
            raw = {'id': 'skill-test', 'model': 'test-double-not-real-api',
                   'choices': [{'message': {'content': answer}, 'finish_reason': 'stop'}]}
            return {'content': answer, 'finish_reason': 'stop', 'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
                    'response_model': raw['model'], 'response_id': raw['id'], 'elapsed_seconds': 0, 'raw': raw}
        self.w.qa_client = fake
        self.server = make_server(self.w, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        for thread in self.w.qa_threads.values():
            thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def cli(self, *args, payload=None, code=0):
        command = [sys.executable, '-B', str(ROOT/'scripts/wiki.py'), '--url', self.base, *args]
        if payload is not None:
            command += ['--input', '-']
        result = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                                text=True, capture_output=True, cwd=self.tmp.name, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_01_status_and_retrieval_from_foreign_cwd(self):
        s = self.cli('status')
        self.assertEqual(s['rules'], 3)
        self.assertEqual(s['configuration']['public_documents'], 15)
        r = self.cli('retrieve', '--question', 'GOV.UK何时补充开始按钮为何使用链接的解释？',
                     '--family', 'GOV.UK', '--role', 'change_log', '--top-k', '3')
        self.assertTrue(any(h['text'].startswith('Oct 2020') and 'Added explanation' in h['text'] for h in r['hits']))
        self.assertEqual(self.calls, [])

    def test_02_complete_maintain_and_reanswer(self):
        entry = next(r for r in self.cli('rules') if r['rule']['id'] == 'WCAG-2.5.8')
        case = deepcopy(self.w._find('cases', 'D01'))
        case['id'] = 'SKILL-ISOLATED-CASE'
        case['exceptions'] = {key: False for key in entry['rule']['exceptions']}
        old = self.cli('judge', payload={'case': case})['judgment']
        self.assertEqual(old['result']['assessment'], 'pass')
        query = ('--question', '当前本地WCAG-2.5.8尺寸是多少？', '--family', '本地规则', '--role', 'local_policy')
        self.cli('ask', *query, code=1)
        self.assertEqual(self.calls, [])
        q = self.cli('ask', *query, '--execute-api', '--request-id', 'skill-test-first')['record']
        self.cli('wait', '--id', q['id'])
        self.cli('ask', *query, '--execute-api', '--request-id', 'skill-test-first')
        self.assertEqual(len(self.calls), 1)
        candidate = self.cli('submit', payload={
            'rule_id': entry['rule']['id'], 'base_rule_sha256': entry['sha256'],
            'patch': {'min_width': 30, 'min_height': 30},
            'material': {'title': 'Skill隔离验收', 'text': '仅在隔离工作区构造本地阈值变更，用于验证CLI完整流程，不是官方规范。',
                         'source_note': '自动化测试构造材料，非实际审核'}})['candidate']
        self.cli('preview', '--id', candidate['id'], code=1)
        self.cli('decide', payload={'candidate_id': candidate['id'], 'decision': 'approve',
                 'reviewer': 'Skill自动验收（非用户本人审核）', 'reason': '隔离工作区验证发布和旧记录保留',
                 'evidence': '明确的构造材料和初始结构化字段'})
        preview = self.cli('preview', '--id', candidate['id'])
        pub = self.cli('publish', '--id', candidate['id'], '--preview-id', preview['preview_id'])['publication']
        self.assertEqual(pub['affected_judgment_ids'], [old['id']])
        self.assertIn(q['id'], pub['affected_qa_ids'])
        new = self.cli('review-judgment', '--id', old['id'])['judgment']
        self.assertEqual(new['result']['assessment'], 'fail')
        self.assertEqual(new['parent_id'], old['id'])
        q2 = self.cli('ask', *query, '--execute-api', '--parent-id', q['id'], '--request-id', 'skill-test-second')['record']
        self.cli('wait', '--id', q2['id'])
        output = Path(self.tmp.name)/'result with spaces.json'
        state = self.cli('export', '--output', str(output))
        self.assertEqual(json.loads(output.read_text()), state)
        self.assertEqual(next(j for j in state['judgments'] if j['id'] == old['id'])['result']['assessment'], 'pass')
        self.assertEqual(next(r for r in state['qa_records'] if r['id'] == q['id'])['freshness'], 'superseded')

    def test_03_local_only_and_missing_id(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/wiki.py'), '--url', 'https://example.com:443', 'status'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.cli('wait', '--id', 'QA-missing', '--timeout', '0', code=1)
        self.assertEqual(self.calls, [])

    def test_04_server_error_stays_error(self):
        def fail(messages):
            raise RuntimeError('隔离测试模拟故障，没有付费调用')
        self.w.qa_client = fail
        q = self.cli('ask', '--question', '当前本地目标尺寸是多少？', '--family', '本地规则',
                     '--execute-api', '--request-id', 'skill-test-failure')['record']
        result = self.cli('wait', '--id', q['id'], code=1)
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['answer'])
        self.assertEqual(len(self.w.state['qa_records']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
