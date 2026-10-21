"""One quiet Gmail scan: Redis config/state, Discord webhook delivery."""
import json
import os
import random
import re
import sys
import time
import uuid
from contextlib import contextmanager
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


class Diagnostic(RuntimeError):
    """Contains only a fixed diagnostic code, never request data."""


@contextmanager
def stage(code):
    try:
        yield
    except Diagnostic:
        raise
    except Exception:
        raise Diagnostic(code) from None


def safe_error(exc):
    # Whitelist the entire code: exception strings can contain secret URLs.
    code = str(exc) if isinstance(exc, Diagnostic) else "UNEXPECTED_ERROR"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        code = "UNEXPECTED_ERROR"
    return "Workflow failed: " + code


class Redis:
    def __init__(self):
        if not all(os.environ.get(k) for k in (
            "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"
        )):
            raise Diagnostic("UPSTASH_SECRETS_MISSING")
        self.url = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
        if urlparse(self.url).scheme != "https":
            raise Diagnostic("UPSTASH_REST_URL_REQUIRES_HTTPS")
        self.session = requests.Session()
        self.session.headers["Authorization"] = "Bearer " + os.environ["UPSTASH_REDIS_REST_TOKEN"]

    def command(self, *args):
        with stage("REDIS_NETWORK_OR_RESPONSE_ERROR"):
            r = self.session.post(self.url, json=list(args), timeout=30, allow_redirects=False)
            if not r.ok:
                raise Diagnostic("REDIS_HTTP_" + str(r.status_code))
            data = r.json()
        if "error" in data or "result" not in data:
            raise Diagnostic("REDIS_COMMAND_ERROR")
        return data["result"]

    def load(self, key):
        raw = self.command("GET", key)
        with stage("REDIS_CONFIG_JSON_INVALID" if key == CONFIG_KEY else "REDIS_STATE_JSON_INVALID"):
            return None if raw is None else json.loads(raw)

    def save(self, key, value):
        if self.command("SET", key, json.dumps(value, ensure_ascii=False)) != "OK":
            raise Diagnostic("REDIS_SAVE_FAILED")


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
    for key, minimum in (("initial_lookback_seconds", 1), ("overlap_seconds", 0)):
        if type(scan.get(key)) is not int or scan[key] < minimum:
            raise ValueError("Invalid scan duration")
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
    try:
        creds.refresh(Request())
    except Exception as exc:
        # Only recognize standard OAuth error codes; never print the response.
        known = {"invalid_grant", "invalid_client", "unauthorized_client", "access_denied"}
        for arg in exc.args:
            if isinstance(arg, dict) and arg.get("error") in known:
                raise Diagnostic("GOOGLE_OAUTH_" + arg["error"].upper()) from None
        raise Diagnostic("GOOGLE_OAUTH_REFRESH_FAILED") from None
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
                raise Diagnostic("GMAIL_HTTP_" + str(r.status_code))
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
    raise Diagnostic("DISCORD_HTTP_" + str(r.status_code))


def deliver(redis, state, url, guard, errors=None):
    failures = False
    for message_id in list(state["pending"]):
        guard()
        try:
            discord_send(url, state["pending"][message_id])
        except Exception as exc:
            failures = True
            if errors is not None:
                errors.append(exc)
            continue
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
        state = {"version": 1, "account": account,
                 "last_scan": end - scan_cfg["initial_lookback_seconds"], "seen": [], "pending": {}}
        redis.save(STATE_KEY, state)
    if (state.get("version") != 1 or state["account"] != account or
            type(state["last_scan"]) is not int or state["last_scan"] > end):
        raise ValueError("State account/schema/clock mismatch")
    seen = set(state["seen"])
    query = f"after:{max(0, state['last_scan'] - scan_cfg['overlap_seconds'])} before:{end}"
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
            if mid in seen:
                continue
            msg = gmail_get(session, "/messages/" + mid, {"format": "raw"})
            if not set(msg.get("labelIds", [])) & set(scan_cfg["exclude_labels"]):
                body = parse_message(msg["raw"])
                text = "\n".join(body.values()).casefold()
                hits = [k for k in scan_cfg["keywords"] if k.casefold() in text]
                if hits:
                    state["pending"][mid] = {"event_id": mid, "sender": body["sender"],
                                             "subject": body["subject"], "matched_keywords": hits}
            state["seen"].append(mid)
            seen.add(mid)
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
    with stage("UPSTASH_CONNECTION_CONFIG_INVALID"):
        redis = Redis()
    raw_config = redis.load(CONFIG_KEY)
    if raw_config is None:
        raise Diagnostic("REDIS_CONFIG_KEY_MISSING")
    with stage("CONFIG_FIELDS_INVALID"):
        cfg = validate_config(raw_config)
    lock = str(uuid.uuid4())
    if redis.command("SET", LOCK_KEY, lock, "NX", "EX", 1200) != "OK":
        raise Diagnostic("SCAN_ALREADY_LOCKED_WAIT_20_MINUTES")
    deadline = time.monotonic() + 1050

    def guard():
        if time.monotonic() > deadline or redis.command("GET", LOCK_KEY) != lock:
            raise Diagnostic("SCAN_LOCK_EXPIRED")

    try:
        state = redis.load(STATE_KEY)
        with stage("GMAIL_AUTH_OR_SCAN_FAILED"), gmail_session(cfg) as session:
            account = gmail_get(session, "/profile")["emailAddress"]
            if state is not None and state.get("account") != account:
                raise Diagnostic("REDIS_STATE_BELONGS_TO_DIFFERENT_ACCOUNT")
            if state is not None:
                deliver(redis, state, cfg["discord"]["webhook_url"], guard)
            state = scan(redis, session, cfg, state, guard)
        errors = []
        if not deliver(redis, state, cfg["discord"]["webhook_url"], guard, errors):
            if errors and isinstance(errors[0], Diagnostic):
                raise errors[0]
            raise Diagnostic("DISCORD_DELIVERY_FAILED")
    finally:
        original_failure = sys.exc_info()[0] is not None
        try:
            redis.command("EVAL", "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, LOCK_KEY, lock)
        except Exception:
            if not original_failure:
                raise Diagnostic("REDIS_LOCK_RELEASE_FAILED") from None


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        sys.exit(1)
