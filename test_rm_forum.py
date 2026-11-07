import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import rm_forum_actions as m

ARTICLE = {"id": 2, "title": "old but newly approved", "url": "https://bbs.robomaster.com/article/2"}
class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(m.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def test_entities_and_attribute_order(self):
        result = m.parse_articles('<a class="articleItem extra" href="/article/2?source=2"><div class="articleItem__title">A &amp; B <b>开源</b></div></a>')
        self.assertEqual(result[0]["title"], "A & B 开源")

    def test_broken_page_fails(self):
        with self.assertRaises(RuntimeError):
            m.parse_articles("<html>maintenance</html>")

    def test_old_or_unknown_age_is_not_discarded(self):
        for age in (None, 168, 10000):
            with patch.object(m, "send_feishu_post", return_value=True) as send:
                state, errors = m.process_articles([{**ARTICLE, "age_hours": age}], {"seen_ids": [1]}, ["hook"])
                self.assertEqual(errors, 0)
                self.assertIn(2, state["seen_ids"])
                send.assert_called_once()

    def test_failure_survives_disappearance_from_page(self):
        with patch.object(m, "send_feishu_post", return_value=False):
            state, errors = m.process_articles([ARTICLE], {"seen_ids": [1]}, ["hook"])
        self.assertEqual(errors, 1)
        self.assertNotIn(2, state["seen_ids"])
        with patch.object(m, "send_feishu_post", return_value=True) as send:
            state, errors = m.process_articles([], state, ["hook"])
        send.assert_called_once()
        self.assertIn(2, state["seen_ids"])
        self.assertEqual(state["pending"], {})

    def test_partial_delivery_only_retries_failed_webhook(self):
        with patch.object(m, "send_feishu_post", side_effect=[True, False]):
            state, _ = m.process_articles([ARTICLE], {"seen_ids": [1]}, ["a", "b"])
        with patch.object(m, "send_feishu_post", return_value=True) as send:
            state, _ = m.process_articles([ARTICLE], state, ["a", "b"])
        self.assertEqual(send.call_args.args[0], "b")
        send.assert_called_once()

    def test_dry_run_never_sends_or_changes_input(self):
        state = {"seen_ids": [1]}
        original = copy.deepcopy(state)
        with patch.object(m, "send_feishu_post") as send:
            m.process_articles([ARTICLE], state, ["hook"], dry_run=True)
        send.assert_not_called()
        self.assertEqual(state, original)

    def test_first_run_and_seen_articles_do_not_send(self):
        with patch.object(m, "send_feishu_post") as send:
            state, _ = m.process_articles([ARTICLE], {"seen_ids": []}, ["hook"])
            m.process_articles([ARTICLE], state, ["hook"])
        send.assert_not_called()

    def test_confirmed_backfill_once(self):
        with patch.object(m, "send_feishu_post", return_value=True) as send:
            state, _ = m.process_articles([], {"seen_ids": [2]}, ["hook"], backfill=[ARTICLE])
            m.process_articles([], state, ["hook"], backfill=[ARTICLE])
        send.assert_called_once()
        self.assertEqual(state["backfilled_ids"], [2])

    def test_missing_or_error_code_is_failure(self):
        for result in ({}, {"code": 19024}, {"StatusCode": 1}):
            response = Mock()
            response.json.return_value = result
            with patch.object(m.requests, "post", return_value=response):
                self.assertFalse(m.send_feishu_post("hook", ARTICLE))

    def test_main_dry_run_never_writes_even_initial_baseline(self):
        with patch.object(m.sys, "argv", ["monitor", "--dry-run"]), \
             patch.object(m, "load_state", return_value={"seen_ids": []}), \
             patch.object(m, "fetch_articles", return_value=[ARTICLE]), \
             patch.object(m, "save_state") as save:
            m.main()
        save.assert_not_called()

if __name__ == "__main__":
    unittest.main()
