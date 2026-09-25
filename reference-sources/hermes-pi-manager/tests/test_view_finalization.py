"""Existing-snapshot contract and view finalization regressions (unit level).

Covers the publication markers that let a viewer know a preview/log is
FINISHED (never "two equal reads"), and the bounded retry that keeps a newer
version from being lost when a snapshot or log write fails once.

Run (from tests/): python3 -m unittest -v test_view_finalization
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import activity as activity_module  # noqa: E402
from activity import ActivityRecorder, load_snapshot  # noqa: E402
from live_transcript import TranscriptRecorder, read_transcript, transcript_path  # noqa: E402


def event(text="line one"):
    return {"type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class ViewFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-final-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_stream_is_open_until_finalized_and_reopens_on_new_events(self):
        recorder = ActivityRecorder(self.tmp / "activity", interval=3600)
        self.addCleanup(recorder.close)
        recorder.observe("pi-fin", event())
        recorder.flush()
        data = load_snapshot(self.tmp / "activity", "pi-fin")
        self.assertEqual(data["publication"], {"state": "open", "seq": data["seq"]})
        recorder.finalize("pi-fin")
        recorder.flush()
        data = load_snapshot(self.tmp / "activity", "pi-fin")
        self.assertEqual(data["publication"], {"state": "complete", "seq": data["seq"]})
        recorder.observe("pi-fin", event("later line"))
        recorder.flush()
        data = load_snapshot(self.tmp / "activity", "pi-fin")
        self.assertEqual(data["publication"]["state"], "open")
        self.assertEqual(data["publication"]["seq"], data["seq"])

    def test_finalize_for_an_unseen_task_is_a_noop(self):
        recorder = ActivityRecorder(self.tmp / "activity", interval=3600)
        self.addCleanup(recorder.close)
        recorder.finalize("pi-never-seen")
        recorder.flush()
        self.assertFalse((self.tmp / "activity").exists())

    def test_failed_snapshot_write_keeps_the_newest_version_for_retry(self):
        recorder = ActivityRecorder(self.tmp / "activity", interval=3600)
        self.addCleanup(recorder.close)
        recorder.observe("pi-retry", event("first"))
        recorder.observe("pi-retry", event("second"))
        with mock.patch.object(activity_module.os, "replace", side_effect=OSError("disk full")):
            failed = recorder.flush()
        self.assertGreaterEqual(failed, 1)
        self.assertEqual(load_snapshot(self.tmp / "activity", "pi-retry"), {})
        recorder.flush()
        data = load_snapshot(self.tmp / "activity", "pi-retry")
        # The retried write publishes the NEWEST state, exactly once.
        self.assertEqual(data["seq"], 2)
        self.assertIn("second", data["text"])

    def test_successful_flush_does_not_resurrect_an_older_version(self):
        recorder = ActivityRecorder(self.tmp / "activity", interval=3600)
        self.addCleanup(recorder.close)
        recorder.observe("pi-once", event("only"))
        self.assertEqual(recorder.flush(), 0)
        data = load_snapshot(self.tmp / "activity", "pi-once")
        self.assertEqual(data["seq"], 1)
        self.assertEqual(recorder.flush(), 0)
        again = load_snapshot(self.tmp / "activity", "pi-once")
        self.assertEqual(again["seq"], 1)

    def test_transcript_retries_a_failed_append_without_losing_or_duplicating(self):
        recorder = TranscriptRecorder(self.tmp / "cli-transcripts", lambda value: value)
        recorder.observe("pi-log", event("hello world"), 1000.0)
        real_append = recorder._append
        calls = {"count": 0}

        def flaky(task_id, data):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("disk full")
            return real_append(task_id, data)

        with mock.patch.object(recorder, "_append", side_effect=flaky):
            self.assertGreaterEqual(recorder.flush(), 1)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(recorder.flush(), 0)
        text = read_transcript(self.tmp / "cli-transcripts", "pi-log")["text"]
        self.assertEqual(text.count("hello world"), 1)

    def test_transcript_final_marker_tracks_generation_and_reopens(self):
        recorder = TranscriptRecorder(self.tmp / "cli-transcripts", lambda value: value)
        recorder.observe("pi-tail", event("last visible line"), 2000.0)
        recorder.flush()
        first = read_transcript(self.tmp / "cli-transcripts", "pi-tail")
        self.assertFalse(first.get("final"))
        recorder.finalize("pi-tail")
        recorder.flush()
        marker = transcript_path(self.tmp / "cli-transcripts", "pi-tail").with_name(
            transcript_path(self.tmp / "cli-transcripts", "pi-tail").name + ".final")
        self.assertTrue(marker.exists())
        done = read_transcript(self.tmp / "cli-transcripts", "pi-tail")
        self.assertTrue(done.get("final"))
        # A later visible event reopens the stream: the marker must go stale.
        recorder.observe("pi-tail", event("more work"), 3000.0)
        recorder.flush()
        self.assertFalse(read_transcript(self.tmp / "cli-transcripts", "pi-tail").get("final"))

    def test_activity_finalize_also_finalizes_the_log_marker(self):
        recorder = ActivityRecorder(self.tmp / "activity", interval=3600)
        self.addCleanup(recorder.close)
        recorder.observe("pi-both", event("both streams"))
        recorder.flush()
        recorder.finalize("pi-both")
        recorder.flush()
        self.assertTrue(read_transcript(self.tmp / "cli-transcripts", "pi-both").get("final"))
        self.assertEqual(load_snapshot(self.tmp / "activity", "pi-both")["publication"]["state"], "complete")


    def test_final_marker_is_not_written_when_the_last_append_fails(self):
        directory = self.tmp / "cli-transcripts"
        recorder = TranscriptRecorder(directory, lambda value: value)
        recorder.observe("pi-late", {"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "first part"}]}}, 1000.0)
        recorder.flush()
        marker = transcript_path(directory, "pi-late").with_name(
            transcript_path(directory, "pi-late").name + ".final")
        self.assertFalse(marker.exists())
        recorder.observe("pi-late", {"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "tail before final"}]}}, 1001.0)
        recorder.finalize("pi-late")
        real = recorder._append
        with mock.patch.object(recorder, "_append", side_effect=OSError("disk gone")):
            self.assertGreaterEqual(recorder.flush(), 1)
        self.assertFalse(marker.exists(), "a failed append must never publish a final marker")
        # the retained record and the pending finalization both survive for the retry
        recorder.flush()
        self.assertTrue(marker.exists())
        data = read_transcript(directory, "pi-late")
        self.assertIn("tail before final", data["text"])
        self.assertTrue(data["final"])

    def test_a_newer_event_during_flush_is_not_lost_or_overwritten(self):
        directory = self.tmp / "activity"
        recorder = ActivityRecorder(directory, interval=3600)
        recorder.observe("pi-race", {"type": "agent_start"})
        real_replace = activity_module.os.replace

        def racing_replace(src, dst):
            # A new event lands WHILE the older version is being written.
            recorder.observe("pi-race", {"type": "agent_start"})
            return real_replace(src, dst)

        with mock.patch.object(activity_module.os, "replace", side_effect=racing_replace):
            recorder.flush()
        # The older flush must not discard the newer version: the next flush
        # publishes it, and the snapshot never goes backwards.
        recorder.flush()
        data = load_snapshot(directory, "pi-race")
        self.assertIsNotNone(data)
        self.assertGreaterEqual(data["seq"], 2)

if __name__ == "__main__":
    unittest.main()
