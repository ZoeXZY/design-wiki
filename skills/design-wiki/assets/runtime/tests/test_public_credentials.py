import os
import unittest
from unittest.mock import patch
import model_client

class PublicCredentialsTests(unittest.TestCase):
    def test_environment_key_only(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ' test-placeholder '}, clear=True):
            self.assertEqual(model_client.load_key(), 'test-placeholder')
            self.assertEqual(model_client.credential_status(), 'environment')

    def test_no_key_does_not_read_files(self):
        with patch.dict(os.environ, {}, clear=True), patch('builtins.open', side_effect=AssertionError('credential file read')):
            self.assertEqual(model_client.credential_status(), 'missing')
            with self.assertRaisesRegex(RuntimeError, 'DEEPSEEK_API_KEY'):
                model_client.load_key()

    def test_blank_key_is_missing(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': '  '}, clear=True):
            self.assertEqual(model_client.credential_status(), 'missing')
            with self.assertRaises(RuntimeError):model_client.load_key()

if __name__ == '__main__':unittest.main()
