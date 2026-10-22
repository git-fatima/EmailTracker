"""One Gmail scan with Redis-backed Discord delivery and dashboard records."""
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from mail_content import parse_message

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
CONFIG_KEY = "config"
STATE_KEY = "gmail-alerts:state"
LOCK_KEY = "gmail-alerts:lock"
DASHBOARD_KEY = "gmail-alerts:dashboard:v1"


class Redis:
    def __init__(self):
        self.url = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
        if urlparse(self.url).scheme != "https":
            raise ValueError("HTTPS required")
        self.session = requests.Session()
        self.session.headers["Authorization"] = "Bearer " + os.environ["UPSTASH_REDIS_REST_TOKEN"]

    def command(self, *args):
        r = self.session.post(self.url, json=list(args), timeout=30, allow_redirects=False)
        r.raise_for_status()
        data = r.json()
        if "error" in data or "result" not in data:
            raise RuntimeError("Redis command failed")
        return data["result"]

    def load(self, key):
        raw = self.command("GET", key)
        return None if raw is None else json.loads(raw)

    def save(self, key, value):
        if self.command("SET", key, json.dumps(value, ensure_ascii=False)) != "OK":
            raise RuntimeError("Redis save failed")


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("Missing config")
    for name in ("client_id", "client_secret", "refresh_token"):
        if not isinstance(cfg["gmail"].get(name), str) or not cfg["gmail"][name].strip():
            raise ValueError("Missing Gmail credential")
    url = urlparse(cfg["discord"]["webhook_url"])
    if (url.scheme != "https" or url.netloc != "discord.com" or
            not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_.-]+", url.path)
            or url.query or url.fragment):
        raise ValueError("Use the standard copied Discord webhook URL")
    scan = cfg["scan"]
    keywords = scan["keywords"]
    if not isinstance(keywords, list) or not keywords or any(
        not isinstance(k, str) or not k.strip() for k in keywords
    ):
        raise ValueError("Invalid keywords")
    if (type(scan.get("initial_lookback_seconds")) is not int or
            scan["initial_lookback_seconds"] < 1):
        raise ValueError("Invalid initial lookback")
    if type(scan.get("include_spam_trash")) is not bool:
        raise ValueError("Invalid spam setting")
    if not isinstance(scan.get("exclude_labels"), list) or any(
        not isinstance(x, str) for x in scan["exclude_labels"]
    ):
        raise ValueError("Invalid excluded labels")
    return cfg


def gmail_session(cfg):
    g = cfg["gmail"]
    creds = Credentials(token=None, token_uri="https://oauth2.googleapis.com/token",
                        scopes=SCOPES, client_id=g["client_id"],
                        client_secret=g["client_secret"], refresh_token=g["refresh_token"])
    creds.refresh(Request())
    return AuthorizedSession(creds)


def gmail_get(session, path, params=None):
    for attempt in range(5):
        try:
            r = session.get(API + path, params=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 4:
                raise
        else:
            if r.ok:
                return r.json()
            reasons = set()
            if r.status_code == 403:
                reasons = {x.get("reason") for x in r.json().get("error", {}).get("errors", [])}
            if (r.status_code not in (429, 500, 502, 503, 504) and not
                    reasons & {"rateLimitExceeded", "userRateLimitExceeded"}) or attempt == 4:
                raise RuntimeError("Gmail request failed")
        time.sleep(2 ** attempt + random.random())


def discord_send(url, alert):
    content = (f"Email alert\nFrom: {alert['sender']}\nSubject: {alert['subject']}\n"
               f"Matched: {', '.join(alert['matched_keywords'])}\nID: {alert['event_id']}")
    # UTF-16 length also stays below Discord's limit for emoji-containing mail.
    content = content.encode("utf-16-le")[:3800].decode("utf-16-le", errors="ignore")
    for attempt in range(3):
        r = requests.post(url, params={"wait": "true"},
                          json={"content": content, "allowed_mentions": {"parse": []}, "flags": 4},
                          timeout=30, allow_redirects=False)
        if r.status_code == 200 and r.json().get("id"):
            return
        if r.status_code == 429 and attempt < 2:
            delay = float(r.json().get("retry_after", 1))
            if not 0 <= delay <= 60:
                break
            time.sleep(delay + 0.1)
            continue
        break
    raise RuntimeError("Discord delivery failed")


def validate_state(state, account, end):
    if (state.get("version") != 2 or state.get("account") != account or
            type(state.get("tracking_start")) is not int or
            not 0 <= state["tracking_start"] <= end or
            type(state.get("last_scan")) is not int or state["last_scan"] > end or
            not isinstance(state.get("posted"), list) or
            not all(isinstance(mid, str) for mid in state["posted"]) or
            not isinstance(state.get("pending"), dict)):
        raise ValueError("State account/schema/clock mismatch")


def deliver(redis, state, url, guard):
    failures = False
    for message_id in list(state["pending"]):
        guard()
        try:
            discord_send(url, state["pending"][message_id])
        except Exception:
            failures = True
            continue
        state["posted"].append(message_id)
        del state["pending"][message_id]
        # Stop if state cannot be saved after a confirmed delivery.
        guard()
        redis.save(STATE_KEY, state)
    return not failures


def scan(redis, session, cfg, state, guard, now=None):
    guard()
    end = int(time.time() if now is None else now)
    account = gmail_get(session, "/profile")["emailAddress"]
    scan_cfg = cfg["scan"]
    if state is None:
        state = {"version": 2, "account": account,
                 "tracking_start": max(0, end - scan_cfg["initial_lookback_seconds"]),
                 "last_scan": 0, "posted": [], "pending": {}}
        redis.save(STATE_KEY, state)
    validate_state(state, account, end)
    posted = set(state["posted"])
    # Keep the original after: boundary so a later keyword can match a prior scan.
    query = f"after:{state['tracking_start']} before:{end}"
    page = None
    while True:
        guard()
        params = {"q": query, "maxResults": 500,
                  "includeSpamTrash": str(scan_cfg["include_spam_trash"]).lower()}
        if page:
            params["pageToken"] = page
        batch = gmail_get(session, "/messages", params)
        for item in batch.get("messages", []):
            guard()
            mid = item["id"]
            msg = gmail_get(session, "/messages/" + mid, {"format": "raw"})
            if not set(msg.get("labelIds", [])) & set(scan_cfg["exclude_labels"]):
                body = parse_message(msg["raw"])
                text = "\n".join(body.values()).casefold()
                hits = [k for k in scan_cfg["keywords"] if k.casefold() in text]
                if hits:
                    alert = {"event_id": mid, "sender": body["sender"],
                             "subject": body["subject"], "matched_keywords": hits}
                    # Gmail internalDate is the received timestamp in milliseconds.
                    received_at = datetime.fromtimestamp(
                        int(msg["internalDate"]) / 1000, tz=timezone.utc
                    ).isoformat()
                    guard()
                    old = redis.command("HGET", DASHBOARD_KEY, mid)
                    merged_keywords = list(hits)
                    if old is not None:
                        previous = json.loads(old)
                        if previous.get("account") != account or previous.get("event_id") != mid:
                            raise ValueError("Dashboard record account/ID mismatch")
                        known = {hit.casefold() for hit in merged_keywords}
                        for keyword in previous["matched_keywords"]:
                            if keyword.casefold() not in known:
                                merged_keywords.append(keyword)
                                known.add(keyword.casefold())
                    record = {**alert, "matched_keywords": merged_keywords,
                              "received_at": received_at, "account": account}
                    # The message ID is the hash field; new keyword matches update it.
                    if old is None or record != previous:
                        redis.command("HSET", DASHBOARD_KEY, mid,
                                      json.dumps(record, ensure_ascii=False))
                    if mid not in posted and mid not in state["pending"]:
                        state["pending"][mid] = alert
                        guard()
                        redis.save(STATE_KEY, state)
        page = batch.get("nextPageToken")
        if not page:
            break
    state["last_scan"] = end
    guard()
    redis.save(STATE_KEY, state)
    return state


def run():
    redis = Redis()
    cfg = validate_config(redis.load(CONFIG_KEY))
    lock = str(uuid.uuid4())
    if redis.command("SET", LOCK_KEY, lock, "NX", "EX", 1200) != "OK":
        raise RuntimeError("Another scan is running")
    deadline = time.monotonic() + 1050

    def guard():
        if time.monotonic() > deadline or redis.command("GET", LOCK_KEY) != lock:
            raise RuntimeError("Lock expired")

    try:
        state = redis.load(STATE_KEY)
        with gmail_session(cfg) as session:
            account = gmail_get(session, "/profile")["emailAddress"]
            if state is not None:
                validate_state(state, account, int(time.time()))
                deliver(redis, state, cfg["discord"]["webhook_url"], guard)
            state = scan(redis, session, cfg, state, guard)
        if not deliver(redis, state, cfg["discord"]["webhook_url"], guard):
            raise RuntimeError("Notifications remain pending")
    finally:
        redis.command("EVAL", "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, LOCK_KEY, lock)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        sys.exit(1)
