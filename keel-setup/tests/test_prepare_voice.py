import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('prepare_voice',Path(__file__).parents[1]/'scripts/prepare_voice.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle=Path(self.directory.name)
        contract=Path(__file__).parents[1]/'reference/integration_profiles.v1.json'
        target=self.bundle/'keel-setup/reference/integration_profiles.v1.json'
        target.parent.mkdir(parents=True)
        target.write_bytes(contract.read_bytes())

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.bundle), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def authenticate_fixture(self):
        checker = self.bundle / 'scripts/check_release_bundle.py'
        checker.parent.mkdir()
        checker.write_text("print('PASS: public setup bundle (fixture)')\n")
        self.git('init', '-q')
        self.git('config', 'user.name', 'Voice test')
        self.git('config', 'user.email', 'voice@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/keelapi/keel-setup.git')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def test_bound_release_reports_preparation_and_gates_only(self):
        pin = self.authenticate_fixture()
        with patch.object(module.subprocess, 'run', wraps=subprocess.run) as calls:
            result = module.prepare(self.bundle, pin, 'elevenlabs_agents_gateway_ws_v1')
        self.assertEqual(result['stage'], 'preparation_only')
        self.assertIn('qualified_pricing_admission', result['unresolved_gates'])
        checker_calls = [call.args[0] for call in calls.call_args_list
                         if any(str(arg).endswith('check_release_bundle.py') for arg in call.args[0])]
        self.assertEqual(len(checker_calls), 1)
        self.assertEqual(checker_calls[0][1], '-I')
        self.assertNotIn('certified', json.dumps(result))

    def test_dirty_or_wrong_revision_stops_before_running_bundle_code(self):
        pin = self.authenticate_fixture()
        with self.assertRaises(ValueError):
            module.prepare(self.bundle, 'b' * 40, 'elevenlabs_agents_gateway_ws_v1')
        marker = self.bundle.parent / (self.bundle.name + '-checker-ran')
        self.addCleanup(marker.unlink, missing_ok=True)
        checker = self.bundle / 'scripts/check_release_bundle.py'
        checker.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.git('update-index', '--assume-unchanged', 'scripts/check_release_bundle.py')
        with self.assertRaises(ValueError):
            module.prepare(self.bundle, pin, 'elevenlabs_agents_gateway_ws_v1')
        self.assertFalse(marker.exists())

    def test_no_direct_audio_or_unsupported_webrtc_as_voice_proof(self):
        pin = self.authenticate_fixture()
        for profile in ['elevenlabs_direct_audio_v1','elevenlabs_agents_webrtc_v1','missing']:
            with self.assertRaises(ValueError):
                module.prepare(self.bundle, pin, profile)


if __name__=='__main__':
    unittest.main()
