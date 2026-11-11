#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'desktop_session', ROOT / 'deploy/ui/rkchat/desktop_session.py')
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


class DesktopSessionTests(unittest.TestCase):
    def test_default_keyboard_is_system(self):
        self.assertEqual(session.keyboard_mode(['--fullscreen']), 'system')

    def test_builtin_keyboard_requires_explicit_option(self):
        self.assertEqual(session.keyboard_mode(['--keyboard', 'builtin']), 'builtin')

    def test_invalid_keyboard_is_rejected(self):
        for args in (['--keyboard'], ['--keyboard', 'invalid']):
            with self.assertRaises(RuntimeError):
                session.keyboard_mode(args)

    def test_active_session_ignores_greeter(self):
        replies = ['c1 106 Debian-gdm seat0\nc3 1001 w seat0\n',
                   'Active=yes\nClass=greeter\nType=wayland\nUser=106\n',
                   'Active=yes\nClass=user\nType=wayland\nUser=1001\n']
        with patch.object(session.subprocess, 'check_output', side_effect=replies), \
             patch.object(session.os, 'getuid', return_value=0):
            props = session.active_desktop()
        self.assertEqual(props['Id'], 'c3')
        self.assertEqual(props['User'], '1001')

    def test_regular_user_cannot_select_another_users_session(self):
        replies = ['c3 1001 w seat0\n',
                   'Active=yes\nClass=user\nType=wayland\nUser=1001\n']
        with patch.object(session.subprocess, 'check_output', side_effect=replies), \
             patch.object(session.os, 'getuid', return_value=1002):
            with self.assertRaisesRegex(RuntimeError, '图形桌面'):
                session.active_desktop()

    def test_missing_desktop_is_an_explicit_error(self):
        with patch.object(session.subprocess, 'check_output', return_value=''):
            with self.assertRaises(RuntimeError):
                session.active_desktop()

    def test_wayland_lock_file_is_not_a_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'wayland-0.lock'
            lock.touch()
            self.assertFalse(session.is_socket(lock))
            self.assertFalse(session.is_socket(Path(tmp) / 'missing'))


if __name__ == '__main__':
    unittest.main()
