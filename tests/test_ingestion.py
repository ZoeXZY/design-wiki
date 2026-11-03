from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import APIError, Workspace, make_server
from ingestion import fingerprint


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'state.json'
        self.w = Workspace(self.path)
        self.examples = {x['id']: x['candidate'] for x in self.w.ingestion_examples()['examples']}

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_five_risks_block_without_mutation(self):
        codes = {'I01':'stale_base', 'I02':'missing_blocks', 'I03':'protected_edit_changed',
                 'I04':'reported_failure', 'I05':'unresolved_conflict'}
        initial = self.w.bootstrap()
        for ident, code in codes.items():
            payload = {'candidate': self.examples[ident]}
            result = self.w.ingestion_check(payload)
            self.assertFalse(result['can_register'], ident)
            self.assertIn(code, [i['code'] for i in result['issues']])
            with self.assertRaises(APIError) as caught:
                self.w.ingestion_register(payload)
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(initial, self.w.bootstrap())

    def test_02_valid_addition_keeps_history_and_survives_restart(self):
        old = self.w.bootstrap()['ingestion_documents'][0]
        result = self.w.ingestion_register({'candidate': self.examples['I06']})
        self.assertEqual(result['record']['before'], old)
        self.assertEqual(result['inspection']['semantic_verification'], 'not_performed')
        self.assertEqual(Workspace(self.path).bootstrap(), self.w.bootstrap())
        self.assertNotEqual(fingerprint(old), result['record']['current_sha256'])
        self.assertEqual(self.w.bootstrap()['rules'], Workspace(Path(self.tmp.name)/'other.json').bootstrap()['rules'])

    def test_03_stale_check_cannot_be_replayed(self):
        candidate = self.examples['I06']
        self.assertTrue(self.w.ingestion_check({'candidate': candidate})['can_register'])
        self.w.ingestion_register({'candidate': candidate})
        with self.assertRaises(APIError):
            self.w.ingestion_register({'candidate': candidate})
        self.assertEqual(len(self.w.bootstrap()['ingestion_history']), 1)

    def test_04_equal_count_replacement_is_not_completeness(self):
        candidate = deepcopy(self.examples['I06'])
        candidate['blocks'] = deepcopy(self.w.bootstrap()['ingestion_documents'][0]['blocks'])
        candidate['blocks'][1]['id'] = 'unrelated-filler'
        result = self.w.ingestion_check({'candidate': candidate})
        self.assertEqual(result['before_count'], result['after_count'])
        self.assertFalse(result['can_register'])
        self.assertIn('requirement', result['missing_ids'])

    def test_05_empty_duplicate_and_blank_are_rejected(self):
        for blocks in ([], [{'id':'a','text':'x'},{'id':'a','text':'y'}]):
            candidate = deepcopy(self.examples['I06']); candidate['blocks'] = blocks
            self.assertFalse(self.w.ingestion_check({'candidate':candidate})['can_register'])
        candidate = deepcopy(self.examples['I06']); candidate['blocks'][0]['text'] = ' '
        with self.assertRaises(ValueError): self.w.ingestion_check({'candidate':candidate})

    def test_06_client_cannot_remove_server_protection(self):
        candidate = deepcopy(self.examples['I03']); candidate['protected_block_ids'] = []
        with self.assertRaises(ValueError): self.w.ingestion_register({'candidate':candidate})
        candidate = deepcopy(self.examples['I05']); candidate['open_conflicts'] = []
        with self.assertRaises(ValueError): self.w.ingestion_register({'candidate':candidate})

    def test_07_missing_source_body_holds_registration(self):
        self.w.state['ingestion_documents'][0]['source_body_available'] = False
        result = self.w.ingestion_check({'candidate':self.examples['I06']})
        self.assertIn('source_unavailable', [x['code'] for x in result['issues']])

    def test_08_old_workspace_migrates_without_losing_judgments(self):
        case = self.w.bootstrap()['cases'][0]
        original = self.w.judge({'case':case})['judgment']
        state = self.w.bootstrap(); del state['ingestion_documents']; del state['ingestion_history']
        self.path.write_text(json.dumps(state, ensure_ascii=False))
        loaded = Workspace(self.path).bootstrap()
        self.assertEqual(loaded['judgments'], [original])
        self.assertEqual(len(loaded['ingestion_documents']), 2)

    def test_09_http_registration_rechecks_and_exports_history(self):
        server = make_server(self.w, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def req(path, data=None):
            request = Request(f'http://127.0.0.1:{server.server_port}'+path,
                              data=None if data is None else json.dumps(data).encode(),
                              headers={'Content-Type':'application/json'})
            try:
                with urlopen(request) as response: return response.status, response.read()
            except HTTPError as e: return e.code, e.read()
        try:
            self.assertEqual(req('/ingestion.html')[0], 200)
            self.assertEqual(req('/api/ingestion/examples')[0], 200)
            self.assertEqual(req('/api/ingestion/register', {'candidate':self.examples['I04']})[0], 409)
            self.assertEqual(req('/api/ingestion/register', {'candidate':self.examples['I06']})[0], 200)
            status, raw = req('/api/export')
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(raw)['ingestion_history']), 1)
            self.assertEqual(req('/api/ingestion/register', {'candidate':self.examples['I06']})[0], 409)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main(verbosity=2)
