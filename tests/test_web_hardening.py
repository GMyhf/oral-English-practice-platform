"""Web 层的几道基本防护：登录后跳转不出站、cookie 属性、退出只收 POST。

跑法同 test_app_modes.py（依赖开发库 data/app.db，测试只写它的副本）：

    python -m unittest tests.test_web_hardening -v
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.security import generate_password_hash  # noqa: E402

import app as app_module  # noqa: E402
import db  # noqa: E402
from test_app_modes import ModeTestBase  # noqa: E402

PASSWORD = "secret-pass-1"


class TestSafeNext(unittest.TestCase):

    def test_site_relative_paths_pass(self):
        for target in ["/practice", "/settings?tab=1", "/result/3#top"]:
            self.assertEqual(app_module.safe_next(target), target)

    def test_anything_that_can_leave_the_site_is_refused(self):
        for target in [
            None, "", "practice", "https://evil.example.com", "//evil.example.com",
            "/\t/evil.example.com",      # 浏览器先删 tab，剩下 //evil
            "/\n/evil.example.com",
            "/\r/evil.example.com",
            "/\\evil.example.com",       # 部分浏览器把反斜杠当斜杠
            "/\x7f/evil.example.com",
        ]:
            self.assertIsNone(app_module.safe_next(target), repr(target))


class TestLoginAndCookies(ModeTestBase):

    def make_user(self):
        username = f"web-test-{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO users(username, password_hash, nickname) VALUES(?,?,?)",
                (username, generate_password_hash(PASSWORD), "测试账号"))
            conn.commit()
        finally:
            conn.close()
        return username

    def login(self, next_value=None):
        self.as_server()
        username = self.make_user()
        url = "/login"
        data = {"username": username, "password": PASSWORD}
        if next_value is not None:
            data["next"] = next_value
        return self.client.post(url, data=data)

    def test_login_ignores_an_offsite_next(self):
        res = self.login("/\t/evil.example.com")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/practice")

    def test_login_keeps_an_onsite_next(self):
        res = self.login("/settings")
        self.assertEqual(res.headers["Location"], "/settings")

    def test_session_cookie_is_samesite_lax(self):
        cookie = self.login().headers["Set-Cookie"]
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_secure_flag_follows_config(self):
        saved = app_module.app.config["SESSION_COOKIE_SECURE"]
        try:
            app_module.app.config["SESSION_COOKIE_SECURE"] = True
            self.assertIn("Secure", self.login().headers["Set-Cookie"])
        finally:
            app_module.app.config["SESSION_COOKIE_SECURE"] = saved

    def test_logout_by_get_is_refused_and_keeps_the_session(self):
        self.login()
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertTrue(self.client.get("/api/me").get_json()["logged_in"])

    def test_logout_by_post_works(self):
        self.login()
        res = self.client.post("/logout")
        self.assertEqual(res.status_code, 302)
        self.assertFalse(self.client.get("/api/me").get_json()["logged_in"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
