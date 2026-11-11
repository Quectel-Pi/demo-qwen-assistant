#!/usr/bin/env python3
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy/ui/rkchat'))
import camera
import rkchat


class FrameTests(unittest.TestCase):
    def stream(self):
        s=camera.PreviewStream(w=4,h=4)
        s._frame=np.full((4,4,3),42,dtype=np.uint8)
        s._frame_time=time.monotonic()
        s._seq=10;s._epoch=2;s._running=True
        s._proc=Mock();s._proc.poll.return_value=None
        s._thread=Mock();s._thread.is_alive.return_value=True
        return s

    def test_stalled_frame_is_not_used_as_new_photo(self):
        s=self.stream();s._frame_time=time.monotonic()-5
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(s.snapshot(str(Path(tmp)/'stale.bmp')))

    def test_snapshot_waits_for_a_newer_frame(self):
        s=self.stream()
        def publish():
            time.sleep(0.03)
            with s._frame_ready:
                s._frame=np.full((4,4,3),99,dtype=np.uint8)
                s._frame_time=time.monotonic();s._seq=11
                s._frame_ready.notify_all()
        with tempfile.TemporaryDirectory() as tmp:
            thread=threading.Thread(target=publish);thread.start()
            result=s.fresh_snapshot(str(Path(tmp)/'fresh.bmp'),min_seq=10,epoch=2,timeout=0.5)
            thread.join(timeout=1)
            self.assertIsNotNone(result)
            self.assertEqual(result['seq'],11)
            self.assertLess(result['age_ms'],1000)
            self.assertEqual((Path(tmp)/'fresh.bmp').read_bytes()[54],99)

    def test_closed_camera_epoch_cannot_supply_a_photo(self):
        s=self.stream()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(s.fresh_snapshot(str(Path(tmp)/'old-session.bmp'),epoch=1))


class UiCameraTests(unittest.TestCase):
    def app(self):
        a=rkchat.ChatApp.__new__(rkchat.ChatApp)
        a.cli=Mock();a.cli.busy=True;a.cam_mode=True;a._cam_closing=False
        a._capture_pending=False;a.cur_bubble=None;a.speak_thread=None
        a.btn_cam=Mock();a.btn_clear=Mock();a.btn_send=Mock();a.entry=Mock()
        a._on_warn=Mock();a.cam=Mock();a.cam.running=True;a.cam.snapshot.return_value=True
        a._add_bubble=Mock();a._start_ask=Mock();a.history=[];a._alive=True
        a.keyboard='builtin';a._capture_token=1;a._snapshot_paths=[]
        a.preview_img=Mock();a._set_preview_visible=Mock();a._active_user_bubble=None
        return a

    def test_close_camera_stays_enabled_during_inference(self):
        a=self.app();a._set_controls_busy(True)
        a.btn_cam.set_sensitive.assert_called_with(True)

    def test_busy_request_is_not_overwritten_by_new_snapshot(self):
        a=self.app();a._submit('又一轮问题')
        a._start_ask.assert_not_called()
        self.assertEqual(a.history,[])

    def test_open_camera_is_blocked_while_busy(self):
        a=self.app();a.cam_mode=False;a.preview_box=Mock();a._set_preview_visible=Mock();a.dev='/dev/not-a-real-camera'
        with patch.object(rkchat.os,'system'),patch.object(rkchat.time,'sleep'):
            a._on_cam_clicked()
        a.cam.start.assert_not_called()
        self.assertFalse(a.cam_mode)

    def test_failed_capture_does_not_send_text_against_old_image(self):
        a=self.app();a.cli.busy=False;a._capture_pending=True
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'failed.bmp');Path(path).touch()
            a._capture_ready(1,'现在看到了什么？',path,None)
            self.assertFalse(Path(path).exists())
        a._start_ask.assert_not_called();self.assertEqual(a.history,[])
        self.assertFalse(a._capture_pending)

    def test_late_capture_callback_after_cancel_is_ignored(self):
        a=self.app();a._capture_pending=False;a._capture_token=2
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'old.bmp');Path(path).touch()
            a._capture_ready(1,'旧问题',path,{'seq':3,'age_ms':1})
            self.assertFalse(Path(path).exists())
        a._start_ask.assert_not_called()

    def test_new_image_ack_keeps_only_the_latest_visual_history(self):
        a=self.app();a.history=[{'q':'old','a':'old image'},{'q':'new','a':None}]
        a._need_replay=True
        a._on_image({'kv_reset':1,'embedding_hash':'new'})
        self.assertEqual(a.history,[{'q':'new','a':None}])
        self.assertFalse(a._need_replay)

    def test_new_photo_does_not_replay_old_visual_transcript(self):
        a=self.app();a._need_replay=True;a._replay_prefix=Mock(return_value='OLD_VISUAL_TRANSCRIPT')
        a._speak_seq=0;a.auto_speak=False;a._set_controls_busy=Mock();a.cli.connected=True
        a._start_ask=rkchat.ChatApp._start_ask.__get__(a,rkchat.ChatApp)
        a._start_ask('现在看到了什么？','/tmp/new-image.bmp')
        sent=a.cli.ask.call_args.args[0]
        self.assertNotIn('OLD_VISUAL_TRANSCRIPT',sent)
        self.assertIn('<image>',sent)


if __name__=='__main__':unittest.main()
