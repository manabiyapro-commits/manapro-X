"""有料note上限と承認ゲートのオフライン検証。Bufferには接続しない。"""
import contextlib
import datetime as dt
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import x_tool as t

URL = "https://note.com/manapro/n/test"
class PolicyTests(unittest.TestCase):
    def posts(self, count, mislabeled=False):
        base = (dt.datetime.now(t.JST) + dt.timedelta(days=14)).replace(hour=20, minute=0)
        texts = ["朝焼けの海岸で貝殻を拾う", "図書館の歴史資料を読む", "野菜を育てて収穫する"]
        return [dict(id=f"T-{i}", datetime=(base + dt.timedelta(days=i)).strftime("%Y-%m-%d %H:%M"),
                     kind="情報提供" if mislabeled else "note紹介", status="未承認",
                     text=texts[i] + "\n" + URL) for i in range(count)]

    def check(self, posts):
        with patch.object(t, "load_history", return_value=[]), patch.object(t, "load_paid_urls", return_value=[URL]), patch.object(t, "load_schedule", return_value={}):
            return t.check(ROOT, posts)

    def test_one_and_two_allowed(self):
        for n in (1, 2):
            errors, warns = self.check(self.posts(n))
            self.assertFalse(any(errors.values()))
        self.assertTrue(warns["(週全体)"])

    def test_three_block_every_post(self):
        posts = self.posts(3)
        errors, _ = self.check(posts)
        self.assertTrue(all(any("最大2本" in e for e in errors[p["id"]]) for p in posts))

    def test_mislabeled_paid_links_still_blocked(self):
        posts = self.posts(3, mislabeled=True)
        errors, _ = self.check(posts)
        self.assertTrue(all(any("最大2本" in e for e in errors[p["id"]]) for p in posts))

    def test_approval_binds_body_and_datetime(self):
        p = self.posts(1)[0]
        original = t.approval_hash(p)
        self.assertNotEqual(original, t.approval_hash(dict(p, text=p["text"] + "変更")))
        self.assertNotEqual(original, t.approval_hash(dict(p, datetime="2030-01-01 20:00")))
        self.assertEqual([], t.approved([p]))

if __name__ == "__main__":
    unittest.main()
