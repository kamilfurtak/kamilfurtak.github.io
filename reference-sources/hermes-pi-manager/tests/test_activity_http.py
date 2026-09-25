"""HTTP API fixture contract; no installed Hermes host or real worker."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from registry_db import Registry


class ActivityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.registry = Registry(hermes_home=self.tmp.name)
        self.addCleanup(self.registry.close)
        self.registry.create_task("pi-view", cwd=self.tmp.name, origin=json.dumps({
            "source": "desktop", "session_id": "parent", "hermes_home": self.tmp.name}))


    def test_api_mount_has_no_manager_side_effects_and_is_no_store(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI/httpx supplied by the Hermes runtime")
        path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
        spec = importlib.util.spec_from_file_location("pi_activity_test_api", path)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/pi-manager")
        with patch.object(api, "_profile_home", return_value=self.home), TestClient(app) as client:
            response = client.get("/api/plugins/pi-manager/activity", params={"task_id": "pi-view", "session_id": "parent"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/plugins/pi-manager/activity", params={
                "task_id": "pi-view", "session_id": "foreign"}).status_code, 404)
            self.assertEqual(client.post("/api/plugins/pi-manager/activity").status_code, 405)
            from live_transcript import TranscriptRecorder
            recorder = TranscriptRecorder(self.registry.path.parent / 'cli-transcripts',
                                          lambda text: text.replace('secret-token', '[redacted]'))
            recorder.observe('pi-view', {'type': 'tool_execution_start', 'toolCallId': 'a',
                                        'toolName': 'bash', 'args': {'command': 'printf secret-token'}}, 1)
            recorder.observe('pi-view', {'type': 'tool_execution_update', 'toolCallId': 'a',
                                        'partialResult': {'content': 'output before tool completion\n'}}, 2)
            recorder.flush()
            params = {'task_id': 'pi-view', 'session_id': 'parent', 'transcript': 'true'}
            full = client.get('/api/plugins/pi-manager/activity', params=params)
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.headers['cache-control'], 'no-store')
            data = full.json()
            self.assertIn('printf [redacted]', data['transcript']['text'])
            self.assertIn('output before tool completion', data['transcript']['text'])
            self.assertNotIn('secret-token', full.text)
            self.assertEqual(client.get('/api/plugins/pi-manager/activity', params={
                **params, 'cursor': data['transcript']['cursor']}).json()['transcript']['text'], '')
            self.assertNotIn('transcript', client.get('/api/plugins/pi-manager/activity', params={
                'task_id': 'pi-view', 'session_id': 'parent'}).json(), 'collapsed cards do not read logs')
            with patch.object(api._transcript, 'read_transcript', side_effect=AssertionError('foreign log opened')):
                self.assertEqual(client.get('/api/plugins/pi-manager/activity', params={
                    **params, 'session_id': 'foreign'}).status_code, 404)
            with patch.object(api._transcript, 'read_transcript', side_effect=OSError('display unavailable')):
                broken = client.get('/api/plugins/pi-manager/activity', params=params).json()
                self.assertEqual(broken['task_id'], 'pi-view')
                self.assertIn('error', broken['transcript'])
