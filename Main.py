import os
import json
import time
import asyncio
import urllib.parse
import uuid
import hashlib
from collections import deque
import tornado.httpclient
import tornado.ioloop
import tornado.web
import tornado.websocket

CONFIG = {
    "max_username_length": 50,
    "max_message_length": 500,
    "heartbeat_timeout": 90,
    "max_game_name_length": 80,
    "max_group_name_length": 50,
    "max_group_members": 50,
    "max_groups_per_user": 20,
    "max_chat_history": 1000,
    "max_admin_dm_history": 1000,
}

ADMIN_IDS = {
    11761417,
    530829101,
    817571515,
    1844177730,
    2624269701,
    2502806181,
    1594235217,
    2845101018,
    417995559,
    2064312726,
    9570736130,
    137002724,
    3572567805,
}

connections = {}
user_data = {}
http_clients = {}

banned_users = set()
muted_until = {}
banned_hwids = set()
known_hwids = {}
group_chats = {}
chat_messages = {}
chat_message_order = deque()
admin_dm_history = deque(maxlen=CONFIG["max_admin_dm_history"])
STATE_FILE = os.environ.get("OCN_STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocn_state.json")).strip()
STATE_SAVE_HANDLE = None

ROBLOX_USER_CACHE = {}
ROBLOX_USER_CACHE_TTL = 6 * 60 * 60
PRESENCE_UPDATE_DELAY = 5.0
PRESENCE_UPDATE_HANDLE = None

def sanitize_text(s, max_len=None):
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    cleaned_chars = []
    for ch in s:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars)
    if max_len is not None and len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned

def normalize_chat_color(value, default="78AAFF"):
    text = sanitize_text(value or "", 16).strip().lstrip("#").upper()
    if len(text) == 6 and all(ch in "0123456789ABCDEF" for ch in text):
        return text
    return default

def normalize_optional_chat_color(value):
    if value is None:
        return None
    text = sanitize_text(value or "", 16).strip().lstrip("#").upper()
    if not text:
        return None
    if len(text) == 6 and all(ch in "0123456789ABCDEF" for ch in text):
        return text
    return None

ADMIN_SECRET = os.environ.get("ADMIN_KEY", "").strip()

def coerce_user_id(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
    return None

def roblox_api_urls(url: str):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    subdomain = None
    for suffix in (".roblox.com", ".roproxy.com", ".rotunnel.com"):
        if host.endswith(suffix):
            subdomain = parsed.netloc[: -len(suffix)]
            break
    if not subdomain:
        return [url]
    return [
        urllib.parse.urlunsplit(parsed._replace(netloc=f"{subdomain}.roproxy.com")),
        urllib.parse.urlunsplit(parsed._replace(netloc=f"{subdomain}.rotunnel.com")),
        urllib.parse.urlunsplit(parsed._replace(netloc=f"{subdomain}.roblox.com")),
    ]

async def fetch_roblox_user(user_id: int):
    now = time.time()
    cached = ROBLOX_USER_CACHE.get(user_id)
    if cached and (now - cached.get("ts", 0)) < ROBLOX_USER_CACHE_TTL:
        return cached.get("name"), cached.get("displayName")

    url = f"https://users.roblox.com/v1/users/{int(user_id)}"
    http_client = tornado.httpclient.AsyncHTTPClient()
    for api_url in roblox_api_urls(url):
        try:
            request = tornado.httpclient.HTTPRequest(
                api_url,
                method="GET",
                connect_timeout=2.0,
                request_timeout=4.0,
                headers={"User-Agent": "NA-Chat/1.0"},
            )
            response = await http_client.fetch(request, raise_error=False)
            if response.code != 200:
                continue
            data = json.loads(response.body.decode("utf-8", errors="ignore"))
            name = sanitize_text(data.get("name") or "", CONFIG["max_username_length"])
            display = sanitize_text(data.get("displayName") or "", CONFIG["max_username_length"])
            if name:
                ROBLOX_USER_CACHE[user_id] = {"ts": time.time(), "name": name, "displayName": display}
                return name, display
        except Exception:
            pass

    return None, None

def get_user_list():
    result = []
    for u, d in user_data.items():
        if connections.get(u) is not d.get("connection"):
            continue
        if d.get("hidden", False):
            continue
        activity_hidden = bool(d.get("activity_hidden", False))
        username = sanitize_text(u, CONFIG["max_username_length"])
        display_name = sanitize_text(d.get("display_name") or "", CONFIG["max_username_length"])
        game_status = sanitize_text(d.get("game_status") or "", CONFIG["max_game_name_length"])
        result.append(
            {
                "username": username,
                "displayName": display_name,
                "userId": d.get("user_id"),
                "admin": bool(d.get("admin", False)),
                "chatColor": normalize_chat_color(d.get("chat_color")),
                "chatColor2": normalize_optional_chat_color(d.get("chat_color2")),
                "game": "Game: Hidden" if activity_hidden else game_status,
                "placeId": None if activity_hidden else d.get("place_id"),
                "jobId": None if activity_hidden else d.get("job_id"),
            }
        )
    return result

def get_user_list_admin():
    result = []
    for u, d in user_data.items():
        if connections.get(u) is not d.get("connection"):
            continue
        username = sanitize_text(u, CONFIG["max_username_length"])
        display_name = sanitize_text(d.get("display_name") or "", CONFIG["max_username_length"])
        game_status = sanitize_text(d.get("game_status") or "", CONFIG["max_game_name_length"])
        result.append(
            {
                "username": username,
                "displayName": display_name,
                "userId": d.get("user_id"),
                "admin": bool(d.get("admin", False)),
                "chatColor": normalize_chat_color(d.get("chat_color")),
                "chatColor2": normalize_optional_chat_color(d.get("chat_color2")),
                "hidden": bool(d.get("hidden", False)),
                "activityHidden": bool(d.get("activity_hidden", False)),
                "game": game_status,
                "placeId": d.get("place_id"),
                "jobId": d.get("job_id"),
                "hwidFingerprint": (d.get("hwid") or "")[:16] or None,
            }
        )
    return result

def is_banned(username: str) -> bool:
    if not username:
        return False
    return username.lower() in banned_users

def ban_user(username: str):
    if username:
        banned_users.add(username.lower())

def unban_user(username: str):
    if username:
        banned_users.discard(username.lower())

def get_ban_list():
    return sorted(banned_users)

def normalize_hwid(value):
    if value is None:
        return None
    try:
        raw = str(value).strip()
    except Exception:
        return None
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

def remember_hwid(username: str, hwid_hash):
    if not username or not hwid_hash:
        return False
    key = username.lower()
    previous = known_hwids.get(key)
    previous_hash = previous.get("hwid") if isinstance(previous, dict) else previous
    previous_name = previous.get("username") if isinstance(previous, dict) else None
    changed = previous_hash != hwid_hash or previous_name != username
    known_hwids[key] = {"username": username, "hwid": hwid_hash}
    return changed

def get_known_hwid(target: str):
    value = sanitize_text(target or "", 128).strip()
    if not value:
        return None
    lower = value.lower()
    if len(lower) == 64 and all(ch in "0123456789abcdef" for ch in lower):
        return lower
    if 8 <= len(lower) < 64 and all(ch in "0123456789abcdef" for ch in lower):
        matches = [digest for digest in banned_hwids if digest.startswith(lower)]
        if len(matches) == 1:
            return matches[0]
    entry = known_hwids.get(lower)
    if isinstance(entry, dict):
        digest = entry.get("hwid")
    else:
        digest = entry
    if isinstance(digest, str) and len(digest) == 64:
        return digest.lower()
    resolved = find_online_username(value)
    if resolved:
        info = user_data.get(resolved, {})
        digest = info.get("hwid")
        if isinstance(digest, str) and len(digest) == 64:
            return digest.lower()
    return None

def is_hwid_banned(hwid_hash) -> bool:
    return bool(hwid_hash and hwid_hash in banned_hwids)

def get_hwid_ban_list():
    aliases = {}
    for entry in known_hwids.values():
        if isinstance(entry, dict):
            digest = entry.get("hwid")
            username = sanitize_text(entry.get("username") or "", CONFIG["max_username_length"]).strip()
        else:
            digest = entry
            username = ""
        if isinstance(digest, str) and digest in banned_hwids and username:
            aliases.setdefault(digest, []).append(username)
    out = []
    for digest in sorted(banned_hwids):
        users = sorted(set(aliases.get(digest, [])), key=str.lower)
        out.append({
            "fingerprint": digest[:16],
            "users": users,
        })
    return out

def get_admin_state():
    return {
        "type": "admin_state",
        "banned": get_ban_list(),
        "muted": get_mute_list(),
        "hwidBanned": get_hwid_ban_list(),
    }

def get_mute_info(username: str):
    if not username:
        return None
    key = username.lower()
    entry = muted_until.get(key)
    if not entry:
        return None
    if isinstance(entry, dict):
        until = entry.get("until")
        reason = entry.get("reason") or ""
    else:
        until = entry
        reason = ""
    try:
        until_val = float(until)
    except Exception:
        muted_until.pop(key, None)
        return None
    now = time.time()
    if now >= until_val:
        muted_until.pop(key, None)
        return None
    return {"until": until_val, "reason": str(reason)}

def mute_user(username: str, duration_seconds: float, reason: str = ""):
    if not username:
        return
    try:
        duration = float(duration_seconds)
    except Exception:
        duration = 0.0
    if duration <= 0:
        unmute_user(username)
        return
    muted_until[username.lower()] = {"until": time.time() + duration, "reason": sanitize_text(reason or "", 200)}

def unmute_user(username: str):
    if username:
        muted_until.pop(username.lower(), None)

def get_mute_list():
    now = time.time()
    out = []
    for name, entry in list(muted_until.items()):
        until = entry.get("until") if isinstance(entry, dict) else entry
        reason = entry.get("reason") if isinstance(entry, dict) else ""
        if until and until > now:
            out.append({"username": name, "until": until, "reason": sanitize_text(reason or "", 200)})
        else:
            muted_until.pop(name, None)
    return out

def broadcast(obj, exclude=None):
    payload = dict(obj)
    payload.setdefault("timestamp", time.time())
    msg = json.dumps(payload, ensure_ascii=False) + "\n"
    for name, ws in list(connections.items()):
        if exclude and name == exclude:
            continue
        try:
            ws.write_message(msg)
        except Exception:
            pass

def broadcast_admin(obj):
    payload = dict(obj)
    payload.setdefault("timestamp", time.time())
    for name, ws in list(connections.items()):
        info = user_data.get(name) or {}
        if info.get("connection") is not ws or not info.get("admin"):
            continue
        try:
            ws.write_message(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

def send_to_user(username, obj):
    ws = connections.get(username)
    if not ws:
        return False
    payload = dict(obj)
    payload.setdefault("timestamp", time.time())
    try:
        ws.write_message(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False

def find_online_username(target):
    if target is None:
        return None
    value = str(target).strip()
    if not value:
        return None
    value_lower = value.lower()
    for name, info in user_data.items():
        if name.lower() == value_lower or str(info.get("user_id")) == value:
            return name
    return None

def _trim_chat_history():
    limit = max(100, int(CONFIG.get("max_chat_history") or 1000))
    while len(chat_message_order) > limit:
        message_id = chat_message_order.popleft()
        chat_messages.pop(message_id, None)

def _chat_reply_snapshot(record):
    if not isinstance(record, dict) or record.get("deleted"):
        return None
    return {
        "messageId": record.get("message_id"),
        "username": record.get("username"),
        "displayName": record.get("display_name") or "",
        "userId": record.get("user_id"),
        "message": record.get("message") or "",
        "edited": bool(record.get("edited", False)),
    }

def _chat_payload(record, event_type="chat"):
    return {
        "type": event_type,
        "messageId": record.get("message_id"),
        "username": record.get("username"),
        "displayName": record.get("display_name") or "",
        "message": record.get("message") or "",
        "timestamp": record.get("timestamp"),
        "userId": record.get("user_id"),
        "admin": bool(record.get("admin", False)),
        "game": record.get("game") or "",
        "chatColor": normalize_chat_color(record.get("chat_color")),
        "chatColor2": normalize_optional_chat_color(record.get("chat_color2")),
        "reply": record.get("reply"),
        "edited": bool(record.get("edited", False)),
        "editedAt": record.get("edited_at"),
    }

def _chat_record_owned_by(record, info):
    if not isinstance(record, dict) or not isinstance(info, dict):
        return False
    return record.get("user_id") == info.get("user_id") and record.get("username") == info.get("username")

def _chat_record_can_modify(record, info):
    if not isinstance(record, dict) or not isinstance(info, dict):
        return False
    return _chat_record_owned_by(record, info) or bool(info.get("admin", False))

def group_snapshot(group):
    return {
        "id": group["id"],
        "name": group["name"],
        "owner": group["owner"],
        "members": sorted(group["members"], key=str.lower),
        "createdAt": group["created_at"],
        "messages": list(group["messages"]),
    }

def groups_for_user(username):
    return [
        group_snapshot(group)
        for group in group_chats.values()
        if username in group["members"]
    ]

def pending_groups_for_user(username):
    return [
        group_snapshot(group)
        for group in group_chats.values()
        if username in group.get("pending", set())
    ]

def push_group_update(group):
    snapshot = group_snapshot(group)
    for member in group["members"]:
        send_to_user(member, {"type": "group_updated", "group": snapshot})

def chat_history_snapshot():
    history = []
    for message_id in chat_message_order:
        record = chat_messages.get(message_id)
        if record and not record.get("deleted"):
            history.append(_chat_payload(record, "chat"))
    return history

def _serialize_state():
    groups = []
    for group in group_chats.values():
        groups.append({
            "id": group["id"],
            "name": group["name"],
            "owner": group["owner"],
            "members": sorted(group["members"], key=str.lower),
            "pending": sorted(group.get("pending", set()), key=str.lower),
            "created_at": group["created_at"],
            "messages": list(group["messages"]),
        })

    history = []
    for message_id in chat_message_order:
        record = chat_messages.get(message_id)
        if record and not record.get("deleted"):
            history.append(record)

    known = []
    for entry in known_hwids.values():
        if isinstance(entry, dict):
            username = sanitize_text(entry.get("username") or "", CONFIG["max_username_length"]).strip()
            digest = entry.get("hwid")
        else:
            username = ""
            digest = entry
        if username and isinstance(digest, str) and len(digest) == 64:
            known.append({"username": username, "hwid": digest})

    return {
        "version": 2,
        "groups": groups,
        "chat_messages": history,
        "banned_users": get_ban_list(),
        "muted": get_mute_list(),
        "banned_hwids": sorted(banned_hwids),
        "known_hwids": known,
    }

def _save_state_now():
    if not STATE_FILE:
        return
    try:
        parent = os.path.dirname(STATE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(_serialize_state(), f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, STATE_FILE)
    except Exception as exc:
        print("state save failed:", exc)

def schedule_state_save():
    global STATE_SAVE_HANDLE
    if STATE_SAVE_HANDLE is not None:
        return

    def flush():
        global STATE_SAVE_HANDLE
        STATE_SAVE_HANDLE = None
        _save_state_now()

    try:
        STATE_SAVE_HANDLE = tornado.ioloop.IOLoop.current().call_later(0.15, flush)
    except Exception:
        STATE_SAVE_HANDLE = None
        _save_state_now()

def _load_state():
    if not STATE_FILE or not os.path.isfile(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        print("state load failed:", exc)
        return

    loaded_banned = payload.get("banned_users") if isinstance(payload, dict) else None
    if isinstance(loaded_banned, list):
        for item in loaded_banned:
            username = sanitize_text(item or "", CONFIG["max_username_length"]).strip().lower()
            if username:
                banned_users.add(username)

    loaded_muted = payload.get("muted") if isinstance(payload, dict) else None
    if isinstance(loaded_muted, list):
        now = time.time()
        for item in loaded_muted:
            if not isinstance(item, dict):
                continue
            username = sanitize_text(item.get("username") or "", CONFIG["max_username_length"]).strip().lower()
            try:
                until = float(item.get("until"))
            except Exception:
                continue
            if username and until > now:
                muted_until[username] = {
                    "until": until,
                    "reason": sanitize_text(item.get("reason") or "", 200),
                }

    loaded_hwid_bans = payload.get("banned_hwids") if isinstance(payload, dict) else None
    if isinstance(loaded_hwid_bans, list):
        for item in loaded_hwid_bans:
            digest = sanitize_text(item or "", 64).strip().lower()
            if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
                banned_hwids.add(digest)

    loaded_known_hwids = payload.get("known_hwids") if isinstance(payload, dict) else None
    if isinstance(loaded_known_hwids, list):
        for item in loaded_known_hwids:
            if not isinstance(item, dict):
                continue
            username = sanitize_text(item.get("username") or "", CONFIG["max_username_length"]).strip()
            digest = sanitize_text(item.get("hwid") or "", 64).strip().lower()
            if username and len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
                known_hwids[username.lower()] = {"username": username, "hwid": digest}

    loaded_groups = payload.get("groups") if isinstance(payload, dict) else None
    if isinstance(loaded_groups, list):
        for item in loaded_groups:
            if not isinstance(item, dict):
                continue
            group_id = sanitize_text(item.get("id") or "", 64).strip()
            name = sanitize_text(item.get("name") or "", CONFIG["max_group_name_length"]).strip()
            owner = sanitize_text(item.get("owner") or "", CONFIG["max_username_length"]).strip()
            if not group_id or not name or not owner:
                continue
            members = {
                sanitize_text(member, CONFIG["max_username_length"]).strip()
                for member in (item.get("members") or [])
                if sanitize_text(member, CONFIG["max_username_length"]).strip()
            }
            members.add(owner)
            pending = {
                sanitize_text(member, CONFIG["max_username_length"]).strip()
                for member in (item.get("pending") or [])
                if sanitize_text(member, CONFIG["max_username_length"]).strip()
            }
            pending.difference_update(members)
            messages = deque(maxlen=100)
            for message in item.get("messages") or []:
                if isinstance(message, dict):
                    text = sanitize_text(message.get("message") or "", CONFIG["max_message_length"])
                    if text:
                        restored = dict(message)
                        restored["message"] = text
                        messages.append(restored)
            try:
                created_at = float(item.get("created_at") or time.time())
            except Exception:
                created_at = time.time()
            group_chats[group_id] = {
                "id": group_id,
                "name": name,
                "owner": owner,
                "members": members,
                "pending": pending,
                "created_at": created_at,
                "messages": messages,
            }

    loaded_history = payload.get("chat_messages") if isinstance(payload, dict) else None
    if isinstance(loaded_history, list):
        for item in loaded_history:
            if not isinstance(item, dict):
                continue
            message_id = sanitize_text(item.get("message_id") or "", 64).strip()
            message = sanitize_text(item.get("message") or "", CONFIG["max_message_length"])
            username = sanitize_text(item.get("username") or "", CONFIG["max_username_length"]).strip()
            if not message_id or not message or not username:
                continue
            record = dict(item)
            record["message_id"] = message_id
            record["message"] = message
            record["username"] = username
            record["deleted"] = False
            chat_messages[message_id] = record
            chat_message_order.append(message_id)
        _trim_chat_history()

_load_state()

def push_presence():
    users = get_user_list()
    admins = get_user_list_admin()

    for name, ws in list(connections.items()):
        info = user_data.get(name) or {}
        if info.get("connection") is not ws or info.get("hidden"):
            continue

        ws.send({"type": "user_list", "users": users})
        if info.get("admin"):
            ws.send({"type": "user_list_admin", "users": admins})


def _flush_presence():
    global PRESENCE_UPDATE_HANDLE
    PRESENCE_UPDATE_HANDLE = None
    push_presence()


def schedule_presence():
    global PRESENCE_UPDATE_HANDLE
    if PRESENCE_UPDATE_HANDLE is not None:
        return
    PRESENCE_UPDATE_HANDLE = tornado.ioloop.IOLoop.current().call_later(
        PRESENCE_UPDATE_DELAY,
        _flush_presence,
    )

class IntegrationHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    async def get(self):
        # A normal GET is used by clients to detect that the service is alive
        # before choosing the HTTP polling fallback.
        if self.request.headers.get("Upgrade", "").lower() != "websocket":
            self.set_status(426)
            self.set_header("Content-Type", "text/plain")
            self.finish("WebSocket upgrade required; use the HTTP fallback endpoints.")
            return
        await super().get()

    def open(self):
        self.username = None
        self.ip = self.request.remote_ip
        print("new connection from", self.ip)

    async def on_message(self, message):
        try:
            data = json.loads(message)
        except Exception:
            self.send_error_msg("Invalid JSON")
            return

        await dispatch_message(self, data)

    async def _dispatch_message(self, data):
        await dispatch_message(self, data)

    def _close_client(self):
        self.on_close()

    def on_close(self):
        if self.username:
            print(self.username, "disconnected")
            self.remove_user()

    def send(self, obj):
        t = obj.get("type") if isinstance(obj, dict) else None
        if t in ("user_list", "user_list_admin"):
            users = obj.get("users") or []
            try:
                total = len(users)
            except Exception:
                total = 0
            max_chunk = 50
            if total > max_chunk:
                chunks = (total + max_chunk - 1) // max_chunk
                snapshot_id = uuid.uuid4().hex
                snapshot_time = time.time()
                for i in range(0, total, max_chunk):
                    chunk = dict(obj)
                    chunk_users = users[i : i + max_chunk]
                    chunk["users"] = chunk_users
                    chunk["chunkIndex"] = i // max_chunk
                    chunk["chunkTotal"] = chunks
                    chunk["snapshotId"] = snapshot_id
                    chunk["timestamp"] = snapshot_time
                    try:
                        self.write_message(json.dumps(chunk, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                return
        payload = dict(obj)
        payload.setdefault("timestamp", time.time())
        try:
            self.write_message(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def send_error_msg(self, msg, code=None, **extra):
        payload = {"type": "error", "message": msg}
        if code:
            payload["code"] = code
        payload.update(extra or {})
        self.send(payload)

    def add_user(self, username, hidden, user_id=None, is_admin=False, game_status=None, place_id=None, job_id=None, activity_hidden=False, display_name="", chat_color="78AAFF", chat_color2=None, hwid_hash=None):
        connections[username] = self
        user_data[username] = {
            "connection": self,
            "session_id": uuid.uuid4().hex,
            "username": username,
            "hidden": hidden,
            "last_seen": time.time(),
            "transport": "http" if hasattr(self, "client_id") else "websocket",
            "user_id": user_id,
            "admin": bool(is_admin),
            "game_status": game_status or "",
            "place_id": place_id,
            "job_id": job_id,
            "activity_hidden": bool(activity_hidden),
            "display_name": display_name or "",
            "chat_color": normalize_chat_color(chat_color),
            "chat_color2": normalize_optional_chat_color(chat_color2),
            "hwid": hwid_hash,
        }

    def remove_user(self):
        u = self.username
        if not u:
            return
        if connections.get(u) is not self:
            self.username = None
            return
        connections.pop(u, None)
        info = user_data.get(u)
        if info and info.get("connection") is self:
            user_data.pop(u, None)
        self.username = None
        schedule_presence()

    async def handle_register(self, data):
        if data.get("is_server") is not True:
            self.send_error_msg("Client outdated / not allowed", code="client_blocked")
            try:
                self.close(4004, "Client blocked")
            except Exception:
                pass
            return

        hidden = bool(data.get("hidden", False))
        user_id = coerce_user_id(data.get("userId"))
        activity_hidden = bool(data.get("activityHidden", False) or data.get("activity_hidden", False))
        raw_game = (data.get("game") or "").strip()
        place_id = data.get("placeId")
        job_id = data.get("jobId")
        chat_color = normalize_chat_color(data.get("chatColor"))
        chat_color2 = normalize_optional_chat_color(data.get("chatColor2"))
        if chat_color2 == chat_color:
            chat_color2 = None
        hwid_hash = normalize_hwid(data.get("hwid"))

        if len(raw_game) > CONFIG["max_game_name_length"]:
            raw_game = raw_game[: CONFIG["max_game_name_length"]]
        raw_game = sanitize_text(raw_game, CONFIG["max_game_name_length"])

        if not user_id or user_id <= 0:
            self.send_error_msg("Missing/invalid userId")
            return

        rb_name, rb_display = await fetch_roblox_user(user_id)
        if not rb_name:
            self.send_error_msg("Could not verify Roblox user")
            return

        username = rb_name
        display_name = rb_display or ""

        if len(username) > CONFIG["max_username_length"]:
            username = username[: CONFIG["max_username_length"]]

        if is_banned(username):
            self.send_error_msg("You are banned from NA Chat")
            try:
                self.close(4003, "Banned from NA Chat")
            except Exception:
                pass
            return

        if is_hwid_banned(hwid_hash):
            self.send_error_msg("This device is HWID banned from NA Chat", code="hwid_banned")
            try:
                self.close(4003, "HWID banned from NA Chat")
            except Exception:
                pass
            return

        if hwid_hash and remember_hwid(username, hwid_hash):
            schedule_state_save()

        if username in connections and connections[username] is not self:
            existing = user_data.get(username, {})
            if existing.get("user_id") != user_id:
                self.send_error_msg("That username is already online")
                return
            try:
                connections[username].close(1000, "Replaced")
            except Exception:
                pass

        is_admin = user_id in ADMIN_IDS

        self.username = username
        self.add_user(
            username,
            hidden,
            user_id=user_id,
            is_admin=is_admin,
            game_status=raw_game,
            place_id=place_id,
            job_id=job_id,
            activity_hidden=activity_hidden,
            display_name=display_name,
            chat_color=chat_color,
            chat_color2=chat_color2,
            hwid_hash=hwid_hash,
        )
        schedule_presence()

        self.send(
            {
                "type": "registered",
                "username": username,
                "displayName": display_name,
                "chatColor": chat_color,
                "chatColor2": chat_color2,
                "token": "dummy_token",
                "hidden": hidden,
                "userId": user_id,
                "admin": is_admin,
                "game": raw_game,
                "placeId": place_id,
                "jobId": job_id,
                "activityHidden": activity_hidden,
            }
        )

        self.send({"type": "chat_history", "messages": chat_history_snapshot()})
        self.send({"type": "user_list", "users": get_user_list()})
        if is_admin:
            self.send({"type": "user_list_admin", "users": get_user_list_admin()})
            self.send(get_admin_state())
            self.send({"type": "admin_dm_history", "messages": list(admin_dm_history)})
        self.send({"type": "group_list", "groups": groups_for_user(username)})
        for group in pending_groups_for_user(username):
            self.send({"type": "group_invite", "group": group})

    def handle_chat(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if info.get("connection") is not self:
            self.send_error_msg("Not registered")
            return
        if info.get("hidden"):
            self.send_error_msg("Hidden users cannot send messages")
            return
        if is_banned(self.username):
            self.send_error_msg("You are banned from NA Chat")
            return
        mute_info = get_mute_info(self.username)
        if mute_info:
            remaining = int(max(0, mute_info["until"] - time.time()))
            msg = f"You are muted in NA Chat ({remaining}s left)"
            if mute_info["reason"]:
                msg += f" - {mute_info['reason']}"
            self.send_error_msg(msg, code="muted", until=mute_info["until"], reason=mute_info["reason"])
            return

        msg = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not msg:
            self.send_error_msg("Message cannot be empty")
            return

        reply = None
        reply_id = sanitize_text(data.get("replyTo") or "", 64).strip()
        if reply_id:
            reply = _chat_reply_snapshot(chat_messages.get(reply_id))
            if not reply:
                self.send_error_msg("Reply target is no longer available", code="reply_target_missing")
                return

        now = time.time()
        message_id = uuid.uuid4().hex[:20]
        record = {
            "message_id": message_id,
            "username": self.username,
            "display_name": info.get("display_name") or "",
            "message": msg,
            "timestamp": now,
            "user_id": info.get("user_id"),
            "admin": bool(info.get("admin", False)),
            "game": info.get("game_status") or "",
            "chat_color": normalize_chat_color(info.get("chat_color")),
            "chat_color2": normalize_optional_chat_color(info.get("chat_color2")),
            "reply": reply,
            "edited": False,
            "edited_at": None,
            "deleted": False,
        }
        chat_messages[message_id] = record
        chat_message_order.append(message_id)
        _trim_chat_history()
        broadcast(_chat_payload(record, "chat"))
        schedule_state_save()

    def handle_edit_message(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if info.get("connection") is not self:
            self.send_error_msg("Not registered")
            return
        message_id = sanitize_text(data.get("messageId") or "", 64).strip()
        record = chat_messages.get(message_id)
        if not record or record.get("deleted"):
            self.send_error_msg("Message not found", code="message_not_found")
            return
        if not _chat_record_can_modify(record, info):
            self.send_error_msg("You are not allowed to edit this message", code="message_not_owned")
            return
        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        record["message"] = message
        record["edited"] = True
        record["edited_at"] = time.time()
        broadcast(_chat_payload(record, "message_edited"))
        schedule_state_save()

    def handle_delete_message(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if info.get("connection") is not self:
            self.send_error_msg("Not registered")
            return
        message_id = sanitize_text(data.get("messageId") or "", 64).strip()
        record = chat_messages.get(message_id)
        if not record or record.get("deleted"):
            self.send_error_msg("Message not found", code="message_not_found")
            return
        if not _chat_record_can_modify(record, info):
            self.send_error_msg("You are not allowed to delete this message", code="message_not_owned")
            return
        record["deleted"] = True
        record["deleted_at"] = time.time()
        broadcast({
            "type": "message_deleted",
            "messageId": message_id,
            "username": record.get("username"),
            "displayName": record.get("display_name") or "",
            "userId": record.get("user_id"),
            "timestamp": record.get("deleted_at"),
        })
        schedule_state_save()

    def handle_heartbeat(self):
        if not self.username:
            return
        d = user_data.get(self.username)
        if d and connections.get(self.username) is self and d.get("connection") is self:
            d["last_seen"] = time.time()
        self.send({"type": "heartbeat_ack"})

    def handle_get_users(self):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        if user_data.get(self.username, {}).get("hidden"):
            self.send_error_msg("Hidden users cannot view user list")
            return
        self.send({"type": "user_list", "users": get_user_list()})

    def handle_get_users_admin(self):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized")
            return
        if info.get("hidden"):
            self.send_error_msg("Hidden users cannot view user list")
            return
        self.send({"type": "user_list_admin", "users": get_user_list_admin()})

    def handle_set_hidden(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        new_hidden = bool(data.get("hidden", False))
        old_hidden = user_data.get(self.username, {}).get("hidden", False)
        if new_hidden == old_hidden:
            return
        user_data[self.username]["hidden"] = new_hidden
        schedule_presence()
        self.send({"type": "hidden_updated", "hidden": new_hidden})

    def handle_set_activity_hidden(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        new_hidden = bool(data.get("activityHidden", data.get("activity_hidden", False)))
        info = user_data.get(self.username)
        if not info:
            self.send_error_msg("Not registered")
            return
        info["activity_hidden"] = new_hidden
        schedule_presence()
        self.send({"type": "activity_updated", "activityHidden": new_hidden})

    def handle_set_chat_color(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username)
        if not info or info.get("connection") is not self:
            self.send_error_msg("Not registered")
            return
        color = normalize_chat_color(data.get("chatColor"))
        color2 = normalize_optional_chat_color(data.get("chatColor2"))
        if color2 == color:
            color2 = None
        info["chat_color"] = color
        info["chat_color2"] = color2
        self.send({"type": "chat_color_updated", "chatColor": color, "chatColor2": color2})

    def handle_typing(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if info.get("hidden"):
            return
        is_typing = bool(data.get("is_typing", False))
        scope = sanitize_text(data.get("scope") or "global", 64)
        now = time.monotonic()
        state = (is_typing, scope)
        previous_state = info.get("typing_state")
        previous_time = float(info.get("typing_broadcast_at") or 0.0)
        if state == previous_state and now - previous_time < 0.5:
            return
        info["typing_state"] = state
        info["typing_broadcast_at"] = now
        broadcast({"type": "typing", "username": self.username, "is_typing": is_typing, "scope": scope}, exclude=self.username)

    def handle_private_chat(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        if user_data.get(self.username, {}).get("hidden"):
            self.send_error_msg("Hidden users cannot send private messages")
            return
        if is_banned(self.username):
            self.send_error_msg("You are banned from NA Chat")
            return
        mute_info = get_mute_info(self.username)
        if mute_info:
            remaining = int(max(0, mute_info["until"] - time.time()))
            msg = f"You are muted in NA Chat ({remaining}s left)"
            if mute_info["reason"]:
                msg += f" - {mute_info['reason']}"
            self.send_error_msg(msg, code="muted", until=mute_info["until"], reason=mute_info["reason"])
            return

        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        target = (data.get("target") or "").strip()

        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        if len(message) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
            return
        if not target:
            self.send_error_msg("Target is required for private message")
            return
        if target == self.username:
            self.send_error_msg("Cannot send private message to yourself")
            return

        resolved_target = find_online_username(target) or target
        stamp = time.time()
        payload = {"type": "private_chat", "from": self.username, "to": resolved_target, "message": message, "timestamp": stamp}
        send_to_user(self.username, payload)
        if not send_to_user(resolved_target, payload):
            self.send_error_msg(f"User '{target}' is not online")
            return

        audit = {
            "from": self.username,
            "to": resolved_target,
            "message": message,
            "timestamp": stamp,
        }
        admin_dm_history.append(audit)
        broadcast_admin({"type": "admin_dm", **audit})

    def handle_group_list(self):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        self.send({"type": "group_list", "groups": groups_for_user(self.username)})

    def handle_group_create(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        if user_data.get(self.username, {}).get("hidden"):
            self.send_error_msg("Hidden users cannot create group chats")
            return
        if is_banned(self.username):
            self.send_error_msg("You are banned from NA Chat")
            return
        if get_mute_info(self.username):
            self.send_error_msg("You are muted in NA Chat")
            return
        if sum(1 for group in group_chats.values() if self.username in group["members"]) >= CONFIG["max_groups_per_user"]:
            self.send_error_msg("Group chat limit reached")
            return

        name = sanitize_text((data.get("name") or "").strip(), CONFIG["max_group_name_length"])
        if not name:
            self.send_error_msg("Group name cannot be empty")
            return

        raw_members = data.get("members")
        if not isinstance(raw_members, list):
            raw_members = []
        members = {self.username}

        group_id = uuid.uuid4().hex[:12]
        group = {
            "id": group_id,
            "name": name,
            "owner": self.username,
            "members": members,
            "pending": set(),
            "created_at": time.time(),
            "messages": deque(maxlen=100),
        }
        group_chats[group_id] = group
        for target in raw_members:
            resolved = find_online_username(target)
            if resolved and resolved != self.username and resolved not in group["members"]:
                if len(group["members"]) + len(group["pending"]) >= CONFIG["max_group_members"]:
                    break
                if sum(1 for item in group_chats.values() if resolved in item["members"] or resolved in item.get("pending", set())) >= CONFIG["max_groups_per_user"]:
                    continue
                group["pending"].add(resolved)
        snapshot = group_snapshot(group)
        for target in group["pending"]:
            send_to_user(target, {"type": "group_invite", "group": snapshot})
        push_group_update(group)
        schedule_state_save()

    def handle_group_invite(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        group = group_chats.get(str(data.get("groupId") or ""))
        if not group:
            self.send_error_msg("Group chat not found")
            return
        if group["owner"] != self.username:
            self.send_error_msg("Only the group owner can invite users")
            return
        pending = group.setdefault("pending", set())
        if len(group["members"]) + len(pending) >= CONFIG["max_group_members"]:
            self.send_error_msg("Group member limit reached")
            return
        target = find_online_username(data.get("target"))
        if not target:
            self.send_error_msg("User is not online")
            return
        if target in group["members"] or target in pending:
            return
        if sum(1 for item in group_chats.values() if target in item["members"] or target in item.get("pending", set())) >= CONFIG["max_groups_per_user"]:
            self.send_error_msg("Target group chat limit reached")
            return
        pending.add(target)
        snapshot = group_snapshot(group)
        send_to_user(target, {"type": "group_invite", "group": snapshot})
        push_group_update(group)
        schedule_state_save()

    def handle_group_accept(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        group_id = str(data.get("groupId") or "")
        group = group_chats.get(group_id)
        if not group:
            self.send_error_msg("Group chat not found")
            return
        pending = group.setdefault("pending", set())
        if self.username not in pending:
            if self.username in group["members"]:
                self.send({"type": "group_updated", "group": group_snapshot(group)})
            else:
                self.send_error_msg("Group invitation not found")
            return
        if len(group["members"]) >= CONFIG["max_group_members"]:
            self.send_error_msg("Group member limit reached")
            return
        if sum(1 for item in group_chats.values() if self.username in item["members"]) >= CONFIG["max_groups_per_user"]:
            self.send_error_msg("Group chat limit reached")
            return
        pending.discard(self.username)
        group["members"].add(self.username)
        push_group_update(group)
        schedule_state_save()

    def handle_group_decline(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        group_id = str(data.get("groupId") or "")
        group = group_chats.get(group_id)
        if not group:
            self.send_error_msg("Group chat not found")
            return
        pending = group.setdefault("pending", set())
        if self.username in pending:
            pending.discard(self.username)
            push_group_update(group)
            schedule_state_save()

    def handle_group_leave(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        group_id = str(data.get("groupId") or "")
        group = group_chats.get(group_id)
        if not group or self.username not in group["members"]:
            self.send_error_msg("Group chat not found")
            return
        if group["owner"] == self.username:
            group_chats.pop(group_id, None)
            recipients = set(group["members"]) | set(group.get("pending", set()))
            for member in recipients:
                if member != self.username:
                    send_to_user(member, {"type": "group_removed", "groupId": group_id})
            schedule_state_save()
            return
        group["members"].discard(self.username)
        push_group_update(group)
        self.send({"type": "group_removed", "groupId": group_id})
        schedule_state_save()

    def handle_group_message(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        if user_data.get(self.username, {}).get("hidden"):
            self.send_error_msg("Hidden users cannot send messages")
            return
        if is_banned(self.username):
            self.send_error_msg("You are banned from NA Chat")
            return
        mute_info = get_mute_info(self.username)
        if mute_info:
            self.send_error_msg("You are muted in NA Chat")
            return
        group = group_chats.get(str(data.get("groupId") or ""))
        if not group or self.username not in group["members"]:
            self.send_error_msg("Group chat not found")
            return
        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        info = user_data.get(self.username, {})
        payload = {
            "type": "group_message",
            "groupId": group["id"],
            "groupName": group["name"],
            "from": self.username,
            "displayName": info.get("display_name") or "",
            "userId": info.get("user_id"),
            "admin": bool(info.get("admin", False)),
            "chatColor": normalize_chat_color(info.get("chat_color")),
            "chatColor2": normalize_optional_chat_color(info.get("chat_color2")),
            "message": message,
        }
        payload["timestamp"] = time.time()
        group["messages"].append({
            "from": self.username,
            "displayName": info.get("display_name") or "",
            "userId": info.get("user_id"),
            "admin": bool(info.get("admin", False)),
            "chatColor": normalize_chat_color(info.get("chat_color")),
            "chatColor2": normalize_optional_chat_color(info.get("chat_color2")),
            "message": message,
            "timestamp": payload["timestamp"],
        })
        for member in group["members"]:
            send_to_user(member, payload)
        schedule_state_save()

    def handle_remote_cmd(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized")
            return
        args = data.get("args")
        if not isinstance(args, list) or not args:
            self.send_error_msg("Invalid args")
            return
        target = data.get("target")
        payload = {"type": "remote_cmd", "fromUserId": info.get("user_id"), "fromUsername": self.username, "args": args, "target": target}
        if target is None or target == "" or target == "all":
            broadcast(payload)
            return
        try:
            target_id = int(target)
        except (TypeError, ValueError):
            self.send_error_msg("Invalid target")
            return
        payload["timestamp"] = time.time()
        msg = json.dumps(payload, ensure_ascii=False) + "\n"
        for name, ws in list(connections.items()):
            uinfo = user_data.get(name, {})
            if uinfo.get("user_id") == target_id:
                try:
                    ws.write_message(msg)
                except Exception:
                    pass

    def _send_targeted_by_user_id(self, payload, target):
        if target is None or target == "" or target == "all":
            broadcast(payload)
            return True
        try:
            target_id = int(target)
        except (TypeError, ValueError):
            self.send_error_msg("Invalid target")
            return False
        payload = dict(payload)
        payload["timestamp"] = time.time()
        msg = json.dumps(payload, ensure_ascii=False) + "\n"
        sent_any = False
        for name, ws in list(connections.items()):
            uinfo = user_data.get(name, {})
            if uinfo.get("user_id") == target_id:
                try:
                    ws.write_message(msg)
                    sent_any = True
                except Exception:
                    pass
        if not sent_any:
            self.send_error_msg("Target not online")
            return False
        return True

    def handle_announcement(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized")
            return
        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        broadcast({"type": "announcement", "from": self.username, "message": message})

    def handle_notify(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized")
            return
        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        duration = data.get("duration")
        try:
            duration = float(duration)
        except Exception:
            duration = 5.0
        if duration < 1:
            duration = 1.0
        if duration > 30:
            duration = 30.0
        target = data.get("target")
        payload = {"type": "notify", "from": self.username, "message": message, "duration": duration}
        self._send_targeted_by_user_id(payload, target)

    def handle_notify2(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized")
            return
        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        target = data.get("target")
        payload = {"type": "notify2", "from": self.username, "message": message}
        self._send_targeted_by_user_id(payload, target)

    def handle_notify3(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized")
            return
        message = sanitize_text((data.get("message") or "").strip(), CONFIG["max_message_length"])
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        target = data.get("target")
        payload = {"type": "notify3", "from": self.username, "message": message}
        self._send_targeted_by_user_id(payload, target)

    def handle_admin_action(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        info = user_data.get(self.username, {})
        if not info.get("admin"):
            self.send_error_msg("Not authorized for admin actions")
            return

        action = (data.get("action") or "").strip().lower()
        target = (data.get("target") or "").strip()
        duration = data.get("duration", 0)

        if not action:
            self.send_error_msg("Missing action")
            return

        if action in ("kick", "ban", "unban", "mute", "unmute", "hwid_ban", "unhwid_ban") and not target:
            self.send_error_msg("Missing target")
            return

        resolved_target = find_online_username(target) or target
        if resolved_target.lower() == self.username.lower() and action in ("ban", "kick", "hwid_ban"):
            self.send_error_msg("You cannot target yourself")
            return

        state_changed = False

        if action == "kick":
            ws = connections.get(resolved_target)
            if not ws:
                self.send_error_msg("Target not found")
            else:
                try:
                    ws.close(4000, "Kicked from NA Chat")
                except Exception:
                    pass
                broadcast({"type": "system", "message": f"{resolved_target} was kicked from NA Chat"})

        elif action == "ban":
            ban_user(resolved_target)
            state_changed = True
            ws = connections.get(resolved_target)
            if ws:
                try:
                    ws.close(4001, "Banned from NA Chat")
                except Exception:
                    pass
            broadcast({"type": "system", "message": f"{resolved_target} was banned from NA Chat"})

        elif action == "unban":
            unban_user(target)
            state_changed = True
            self.send({"type": "system", "message": f"{target} was unbanned from NA Chat"})

        elif action == "mute":
            try:
                duration = float(duration or 300)
            except Exception:
                duration = 300.0
            if duration <= 0:
                duration = 300.0
            raw_reason = (data.get("reason") or "").strip()
            reason = sanitize_text(raw_reason, 200)
            mute_user(resolved_target, duration, reason=reason)
            state_changed = True
            reason_suffix = f" - {reason}" if reason else ""
            broadcast({"type": "system", "message": f"{resolved_target} was muted in NA Chat ({int(duration)}s){reason_suffix}"})

        elif action == "unmute":
            unmute_user(target)
            state_changed = True
            broadcast({"type": "system", "message": f"{target} was unmuted in NA Chat"})

        elif action == "hwid_ban":
            digest = get_known_hwid(resolved_target)
            if not digest:
                self.send_error_msg("No HWID is available for that user; their executor may not expose gethwid()", code="hwid_unavailable")
                return
            banned_hwids.add(digest)
            state_changed = True
            ws = connections.get(resolved_target)
            if ws:
                try:
                    ws.close(4003, "HWID banned from NA Chat")
                except Exception:
                    pass
            broadcast({"type": "system", "message": f"{resolved_target} was HWID banned from NA Chat"})

        elif action == "unhwid_ban":
            digest = get_known_hwid(target)
            if not digest or digest not in banned_hwids:
                self.send_error_msg("HWID ban not found", code="hwid_ban_not_found")
                return
            banned_hwids.discard(digest)
            state_changed = True
            self.send({"type": "system", "message": f"HWID ban removed for {target}"})

        elif action == "purge":
            try:
                count = int(data.get("count") or duration or 1)
            except Exception:
                count = 1
            count = max(1, min(CONFIG["max_chat_history"], count))
            purged = []
            for message_id in reversed(chat_message_order):
                record = chat_messages.get(message_id)
                if not record or record.get("deleted"):
                    continue
                record["deleted"] = True
                record["deleted_at"] = time.time()
                purged.append(message_id)
                if len(purged) >= count:
                    break
            if purged:
                broadcast({
                    "type": "messages_purged",
                    "messageIds": purged,
                    "count": len(purged),
                    "by": self.username,
                })
                schedule_state_save()
            self.send({"type": "system", "message": f"Purged {len(purged)} chat message(s)"})

        elif action == "refresh":
            pass
        else:
            self.send_error_msg("Unknown admin action")
            return

        if state_changed:
            schedule_state_save()
            broadcast_admin(get_admin_state())
        else:
            self.send(get_admin_state())


async def dispatch_message(client, data):
    if not isinstance(data, dict):
        client.send_error_msg("Invalid JSON")
        return

    t = data.get("type")
    if t == "register":
        await client.handle_register(data)
    elif t == "chat":
        client.handle_chat(data)
    elif t == "edit_message":
        client.handle_edit_message(data)
    elif t == "delete_message":
        client.handle_delete_message(data)
    elif t == "heartbeat":
        client.handle_heartbeat()
    elif t == "get_users":
        client.handle_get_users()
    elif t == "get_users_admin":
        client.handle_get_users_admin()
    elif t == "set_hidden":
        client.handle_set_hidden(data)
    elif t == "set_activity_hidden":
        client.handle_set_activity_hidden(data)
    elif t == "set_chat_color":
        client.handle_set_chat_color(data)
    elif t == "remote_cmd":
        client.handle_remote_cmd(data)
    elif t == "typing":
        client.handle_typing(data)
    elif t == "private_chat":
        client.handle_private_chat(data)
    elif t == "group_list":
        client.handle_group_list()
    elif t == "group_create":
        client.handle_group_create(data)
    elif t == "group_invite":
        client.handle_group_invite(data)
    elif t == "group_accept":
        client.handle_group_accept(data)
    elif t == "group_decline":
        client.handle_group_decline(data)
    elif t == "group_leave":
        client.handle_group_leave(data)
    elif t == "group_message":
        client.handle_group_message(data)
    elif t == "announcement":
        client.handle_announcement(data)
    elif t == "notify":
        client.handle_notify(data)
    elif t == "notify2":
        client.handle_notify2(data)
    elif t == "notify3":
        client.handle_notify3(data)
    elif t == "admin_action":
        client.handle_admin_action(data)
    else:
        client.send_error_msg("Unknown type: " + str(t))


class HttpClient(IntegrationHandler):
    """A small adapter that gives HTTP polling clients the same interface as WebSockets."""

    def __init__(self, client_id, ip):
        self.client_id = client_id
        self.ip = ip
        self.username = None
        self.closed = False
        self.last_seen = time.time()
        self.queue = deque()
        self.queue_event = asyncio.Event()

    def write_message(self, message, *args, **kwargs):
        if not self.closed:
            self.queue.append(message)
            self.queue_event.set()

    def close(self, code=None, reason=None):
        if self.closed:
            return
        self.closed = True
        self.queue_event.set()
        http_clients.pop(self.client_id, None)
        self.on_close()


def decode_request_body(request):
    try:
        raw = request.body.decode("utf-8") if request.body else "{}"
        data = json.loads(raw)
    except Exception:
        raise tornado.web.HTTPError(400, reason="Invalid JSON")
    if not isinstance(data, dict):
        raise tornado.web.HTTPError(400, reason="JSON object required")
    return data


class AxxumRegisterHandler(tornado.web.RequestHandler):
    async def post(self):
        data = decode_request_body(self.request)
        client_id = uuid.uuid4().hex
        client = HttpClient(client_id, self.request.remote_ip)
        http_clients[client_id] = client
        await dispatch_message(client, data)
        client.last_seen = time.time()
        self.set_header("Content-Type", "application/json")
        self.write({"clientId": client_id})


class AxxumPollHandler(tornado.web.RequestHandler):
    async def get(self):
        client_id = self.get_query_argument("clientId", default="")
        client = http_clients.get(client_id)
        if not client or client.closed:
            self.set_status(404)
            self.finish("Unknown clientId")
            return

        client.last_seen = time.time()

        if not client.queue:
            client.queue_event.clear()
            if not client.queue and not client.closed:
                try:
                    await asyncio.wait_for(client.queue_event.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    pass

        if client.closed:
            self.set_status(404)
            self.finish("Unknown clientId")
            return

        client.last_seen = time.time()
        messages = []
        while client.queue:
            raw = client.queue.popleft()
            try:
                messages.append(json.loads(raw))
            except Exception:
                continue

        if not client.queue:
            client.queue_event.clear()

        if not messages:
            self.set_status(204)
            self.set_header("Cache-Control", "no-store")
            self.finish()
            return

        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-store")
        self.write(json.dumps(messages, ensure_ascii=False))


class AxxumSendHandler(tornado.web.RequestHandler):
    async def post(self):
        client_id = self.get_query_argument("clientId", default="")
        client = http_clients.get(client_id)
        if not client or client.closed:
            self.set_status(404)
            self.finish("Unknown clientId")
            return

        data = decode_request_body(self.request)
        client.last_seen = time.time()
        await dispatch_message(client, data)
        self.write("OK")


class AxxumDisconnectHandler(tornado.web.RequestHandler):
    def post(self):
        client_id = self.get_query_argument("clientId", default="")
        client = http_clients.get(client_id)
        if client:
            client.close(1000, "Client disconnected")
        self.write("OK")

class StatsHandler(tornado.web.RequestHandler):
    def get(self):
        websocket_users = 0
        http_users = 0
        online = 0
        for name, info in user_data.items():
            if connections.get(name) is not info.get("connection"):
                continue
            online += 1
            if info.get("transport") == "http":
                http_users += 1
            else:
                websocket_users += 1

        queued_http_messages = sum(len(client.queue) for client in http_clients.values() if not client.closed)
        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-store")
        self.write({
            "online": online,
            "websocket": websocket_users,
            "httpFallback": http_users,
            "httpClients": sum(1 for client in http_clients.values() if not client.closed),
            "queuedHttpMessages": queued_http_messages,
            "presencePending": PRESENCE_UPDATE_HANDLE is not None,
        })


class HealthHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("OK")

def make_app():
    return tornado.web.Application([
        (r"/axxum/?$", IntegrationHandler),
        (r"/axxum/register$", AxxumRegisterHandler),
        (r"/axxum/poll$", AxxumPollHandler),
        (r"/axxum/send$", AxxumSendHandler),
        (r"/axxum/disconnect$", AxxumDisconnectHandler),
        (r"/stats$", StatsHandler),
        (r"/healthz$", HealthHandler),
    ])

def cleanup_inactive_users():
    timeout = CONFIG["heartbeat_timeout"]
    now = time.time()
    to_remove = []
    for name, data in list(user_data.items()):
        ws = connections.get(name)
        if not ws or data.get("connection") is not ws:
            continue
        last_seen = data.get("last_seen", now)
        if now - last_seen > timeout:
            to_remove.append((name, ws))

    for name, ws in to_remove:
        print("Removing inactive user", name)
        if connections.get(name) is ws:
            connections.pop(name, None)
        info = user_data.get(name)
        if info and info.get("connection") is ws:
            user_data.pop(name, None)
        try:
            ws.close(1000, "Inactive timeout")
        except Exception:
            pass

    for client_id, client in list(http_clients.items()):
        if client.closed or now - client.last_seen > timeout:
            client.close(1000, "Inactive timeout")

    if to_remove:
        schedule_presence()

if __name__ == "__main__":
    app = make_app()
    port = int(os.environ.get("PORT", "8000"))
    app.listen(port, address="0.0.0.0")
    tornado.ioloop.PeriodicCallback(cleanup_inactive_users, 10000).start()
    print("=" * 50)
    print(f"Server started on port {port} ..")
    print("=" * 50)
    tornado.ioloop.IOLoop.current().start()
