import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import workflow as w


class FakeRedis:
    def __init__(self):
        self.data = {}

    def save(self, key, value):
        self.data[key] = copy.deepcopy(value)


class Tests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.cfg = json.loads((Path(w.__file__).parent / "config.example.json").read_text())
        self.cfg["discord"]["webhook_url"] = "https://discord.com/api/webhooks/123/fake-token"
        self.guard = lambda: None

    def get(self, session, path, params=None):
        if path == "/profile":
            return {"emailAddress": "test@example.com"}
        if path == "/messages":
            if params.get("pageToken") == "next":
                return {"messages": [{"id": "2"}]}
            return {"messages": [{"id": "1"}], "nextPageToken": "next"}
        raw = "From: Test\r\nSubject: COGNIZANT @everyone\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Accenture &amp; careers</p>"
        return {"raw": base64.urlsafe_b64encode(raw.encode()).decode(), "labelIds": ["INBOX"]}

    def test_config(self):
        self.assertIs(w.validate_config(self.cfg), self.cfg)
        self.cfg["discord"]["webhook_url"] = "https://example.com/no"
        with self.assertRaises(ValueError):
            w.validate_config(self.cfg)

    def test_scan_pagination_and_dedup(self):
        with patch.object(w, "gmail_get", side_effect=self.get) as get:
            state = w.scan(self.redis, None, self.cfg, None, self.guard, now=100000)
            self.assertEqual(len(state["pending"]), 2)
            w.scan(self.redis, None, self.cfg, state, self.guard, now=103600)
            gets = [c for c in get.call_args_list if c.args[1].startswith("/messages/")]
            self.assertEqual(len(gets), 2)
        self.assertEqual(self.redis.data[w.STATE_KEY]["last_scan"], 103600)
        self.assertNotIn("body", state["pending"]["1"])

    def test_partial_failure_checkpoint(self):
        def fail(session, path, params=None):
            if path == "/messages/2":
                raise RuntimeError("test failure")
            return self.get(session, path, params)
        with patch.object(w, "gmail_get", side_effect=fail):
            with self.assertRaises(RuntimeError):
                w.scan(self.redis, None, self.cfg, None, self.guard, now=100000)
        state = self.redis.data[w.STATE_KEY]
        self.assertEqual(state["last_scan"], 13600)
        self.assertEqual(state["seen"], ["1"])
        with patch.object(w, "gmail_get", side_effect=self.get):
            w.scan(self.redis, None, self.cfg, state, self.guard, now=103600)
        self.assertEqual(len(state["seen"]), 2)

    def test_failed_delivery_retained(self):
        state = {"pending": {"a": {"event_id": "a"}}}
        self.redis.save(w.STATE_KEY, state)
        with patch.object(w, "discord_send", side_effect=RuntimeError()):
            self.assertFalse(w.deliver(self.redis, state, "url", self.guard))
        self.assertIn("a", self.redis.data[w.STATE_KEY]["pending"])
        with patch.object(w, "discord_send"):
            self.assertTrue(w.deliver(self.redis, state, "url", self.guard))
        self.assertEqual(self.redis.data[w.STATE_KEY]["pending"], {})

    def test_discord_wait_mentions_and_rate_limit(self):
        alert = {"event_id": "1", "sender": "@everyone", "subject": "x" * 2500,
                 "matched_keywords": ["x"]}
        replies = [Mock(status_code=429, json=lambda: {"retry_after": 0.5}),
                   Mock(status_code=200, json=lambda: {"id": "confirmed"})]
        with patch.object(w.requests, "post", side_effect=replies) as post:
            with patch.object(w.time, "sleep"):
                w.discord_send("url", alert)
        self.assertEqual(post.call_args.kwargs["params"], {"wait": "true"})
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertLessEqual(len(body["content"]), 2000)

    def test_unconfirmed_delivery_fails(self):
        alert = {"event_id": "a", "sender": "a", "subject": "a", "matched_keywords": []}
        with patch.object(w.requests, "post", return_value=Mock(status_code=204)):
            with self.assertRaises(RuntimeError):
                w.discord_send("url", alert)

    def test_redis_json_no_base64(self):
        with patch.dict(w.os.environ, {"UPSTASH_REDIS_REST_URL": "https://test.upstash.io",
                                      "UPSTASH_REDIS_REST_TOKEN": "fake"}):
            redis = w.Redis()
        with patch.object(redis.session, "post", return_value=Mock(json=lambda: {"result": "OK"})) as post:
            redis.save("config", self.cfg)
        command = post.call_args.kwargs["json"]
        self.assertEqual(command[:2], ["SET", "config"])
        self.assertEqual(json.loads(command[2]), self.cfg)

    def test_wrong_account_rejected(self):
        state = {"version": 1, "account": "other@example.com", "last_scan": 1,
                 "seen": [], "pending": {}}
        with patch.object(w, "gmail_get", side_effect=self.get):
            with self.assertRaises(ValueError):
                w.scan(self.redis, None, self.cfg, state, self.guard, now=100000)


if __name__ == "__main__":
    unittest.main()
