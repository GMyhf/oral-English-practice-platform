"""init_db.py --reset 的护栏，以及 SECRET_KEY 占位符拒绝启动。

不跑完整的 init_db（要扫上万个音频文件），只测确认、备份、参数解析这几块；
真实数据库和录音目录一律不碰，全在临时目录里造。

    python -m unittest tests.test_init_db_reset -v
"""

import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
from scripts import init_db  # noqa: E402


class TestConfirmReset(unittest.TestCase):

    def test_yes_skips_the_prompt(self):
        self.assertTrue(init_db.confirm_reset(assume_yes=True))

    def test_non_interactive_without_yes_is_refused(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertFalse(init_db.confirm_reset(assume_yes=False))

    def test_interactive_needs_the_exact_word(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            for typed, expected in [("RESET", True), (" RESET ", True),
                                    ("reset", False), ("y", False), ("", False)]:
                with mock.patch("builtins.input", return_value=typed):
                    self.assertEqual(init_db.confirm_reset(False), expected, repr(typed))


class TestParseArgs(unittest.TestCase):

    def test_unknown_flag_is_an_error_not_ignored(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            init_db.parse_args(["--rest"])

    def test_help_exits_before_touching_anything(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as ctx:
            init_db.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class TestBackupBeforeReset(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-reset-test-")
        self.db_path = os.path.join(self.tmp, "app.db")
        self.rec_dir = os.path.join(self.tmp, "recordings")
        os.makedirs(self.rec_dir)
        with open(os.path.join(self.rec_dir, "u1_abc.webm"), "wb") as fh:
            fh.write(b"old")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("INSERT INTO users(username) VALUES('old-student')")
        conn.commit()
        conn.close()
        self.patches = [
            mock.patch.object(init_db, "DB_PATH", self.db_path),
            mock.patch.object(config, "RECORDING_DIR", self.rec_dir),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_database_and_recordings_are_kept_aside(self):
        conn = sqlite3.connect(self.db_path)
        try:
            db_backup, rec_moved = init_db.backup_before_reset(conn)
        finally:
            conn.close()

        self.assertTrue(db_backup.startswith(self.db_path + ".bak-"))
        backup = sqlite3.connect(db_backup)
        try:
            self.assertEqual(backup.execute("SELECT username FROM users").fetchone()[0],
                             "old-student")
        finally:
            backup.close()

        # 原录音目录挪走了：reset 后新的 1 号用户不会「继承」旧 1 号用户的 u1_ 录音
        self.assertFalse(os.path.exists(self.rec_dir))
        self.assertTrue(os.path.exists(os.path.join(rec_moved, "u1_abc.webm")))

    def test_empty_recordings_dir_is_left_alone(self):
        os.remove(os.path.join(self.rec_dir, "u1_abc.webm"))
        conn = sqlite3.connect(self.db_path)
        try:
            _, rec_moved = init_db.backup_before_reset(conn)
        finally:
            conn.close()
        self.assertIsNone(rec_moved)
        self.assertTrue(os.path.isdir(self.rec_dir))


class TestSecretKeyPlaceholder(unittest.TestCase):
    """在子进程里 import config，避免污染本进程已经加载的配置。"""

    def import_config_with(self, secret):
        env = dict(os.environ, SECRET_KEY=secret)
        return subprocess.run([sys.executable, "-c", "import config"], cwd=BASE_DIR,
                              env=env, capture_output=True, text=True)

    def test_template_placeholder_refuses_to_start(self):
        result = self.import_config_with("{{GENERATE_A_RANDOM_64_CHAR_HEX_STRING}}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("占位符", result.stderr)

    def test_real_key_is_accepted(self):
        result = self.import_config_with("a" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
