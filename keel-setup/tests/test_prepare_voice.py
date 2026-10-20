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

    def runner(self, args, **kwargs):
        self.assertNotIn('shell',kwargs)
        stdout='a'*40 if 'rev-parse' in args else ''
        return subprocess.CompletedProcess(args,0,stdout=stdout)

    def test_bound_release_reports_preparation_and_gates_only(self):
        with patch.object(module.subprocess,'run',side_effect=self.runner) as calls:
            result=module.prepare(self.bundle,'a'*40,'elevenlabs_agents_gateway_ws_v1')
        self.assertEqual(result['stage'],'preparation_only')
        self.assertIn('qualified_pricing_admission',result['unresolved_gates'])
        self.assertEqual(calls.call_count,3)
        self.assertNotIn('certified',json.dumps(result))

    def test_dirty_or_wrong_revision_stops_before_running_bundle_code(self):
        for outputs in [('b'*40,),('a'*40,' M keel-setup/scripts/prepare_voice.py')]:
            replies=iter(outputs)
            def changed(args,**_kwargs):
                return subprocess.CompletedProcess(args,0,stdout=next(replies))
            with patch.object(module.subprocess,'run',side_effect=changed) as calls:
                with self.assertRaises(ValueError):
                    module.prepare(self.bundle,'a'*40,'elevenlabs_agents_gateway_ws_v1')
                self.assertLessEqual(calls.call_count,2)

    def test_no_direct_audio_or_unsupported_webrtc_as_voice_proof(self):
        with patch.object(module.subprocess,'run',side_effect=self.runner):
            for profile in ['elevenlabs_direct_audio_v1','elevenlabs_agents_webrtc_v1','missing']:
                with self.assertRaises(ValueError):
                    module.prepare(self.bundle,'a'*40,profile)


if __name__=='__main__':
    unittest.main()
