import os
import json
import time
import urllib.request
import tornado.ioloop
import tornado.web
import tornado.websocket

CONFIG = {
    "max_username_length": 50,
    "max_message_length": 500,
    "heartbeat_timeout": 90,
    "max_game_name_length": 80,
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
    2019160453,
    417995559,
}

connections = {}
user_data = {}

banned_users = set()
muted_until = {}

ROBLOX_USER_CACHE = {}
ROBLOX_USER_CACHE_TTL = 6 * 60 * 60

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


def fetch_roblox_user(user_id: int):
    now = time.time()
    cached = ROBLOX_USER_CACHE.get(user_id)
    if cached and (now - cached.get("ts", 0)) < ROBLOX_USER_CACHE_TTL:
        return cached.get("name"), cached.get("displayName")

    url = f"https://users.roblox.com/v1/users/{int(user_id)}"
    try:
        with urllib.request.urlopen(url, timeout=4.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        name = data.get("name")
        display = data.get("displayName")
        if isinstance(name, str) and name:
            ROBLOX_USER_CACHE[user_id] = {
                "ts": now,
                "name": name,
                "displayName": (display if isinstance(display, str) else ""),
            }
            return name, (display if isinstance(display, str) else "")
    except Exception:
        pass

    return None, None


def get_user_list():
    result = []
    for u, d in user_data.items():
        if d.get("hidden", False):
            continue
        activity_hidden = bool(d.get("activity_hidden", False))
        game_status = d.get("game_status") or ""
        result.append({
            "username": u,
            "displayName": d.get("display_name") or "",
            "userId": d.get("user_id"),
            "admin": bool(d.get("admin", False)),
            "game": "Game: Hidden" if activity_hidden else game_status,
            "placeId": None if activity_hidden else d.get("place_id"),
            "jobId": None if activity_hidden else d.get("job_id"),
        })
    return result


def get_user_list_admin():
    result = []
    for u, d in user_data.items():
        result.append({
            "username": u,
            "displayName": d.get("display_name") or "",
            "userId": d.get("user_id"),
            "admin": bool(d.get("admin", False)),
            "hidden": bool(d.get("hidden", False)),
            "activityHidden": bool(d.get("activity_hidden", False)),
            "game": d.get("game_status") or "",
            "placeId": d.get("place_id"),
            "jobId": d.get("job_id"),
        })
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
    muted_until[username.lower()] = {"until": time.time() + duration, "reason": (reason or "")[:200]}


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
            out.append({"username": name, "until": until, "reason": reason or ""})
        else:
            muted_until.pop(name, None)
    return out


def broadcast(obj, exclude=None):
    obj["timestamp"] = time.time()
    msg = json.dumps(obj)
    for name, ws in list(connections.items()):
        if exclude and name == exclude:
            continue
        try:
            ws.write_message(msg)
        except Exception:
            pass


def send_to_user(username, obj):
    ws = connections.get(username)
    if not ws:
        return False
    obj = dict(obj)
    obj["timestamp"] = time.time()
    try:
        ws.write_message(json.dumps(obj))
        return True
    except Exception:
        return False


class IntegrationHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        self.username = None
        self.ip = self.request.remote_ip
        print("new connection from", self.ip)

    def on_message(self, message):
        try:
            data = json.loads(message)
        except Exception:
            self.send_error_msg("Invalid JSON")
            return

        t = data.get("type")

        if t == "register":
            self.handle_register(data)
        elif t == "chat":
            self.handle_chat(data)
        elif t == "heartbeat":
            self.handle_heartbeat()
        elif t == "get_users":
            self.handle_get_users()
        elif t == "get_users_admin":
            self.handle_get_users_admin()
        elif t == "set_hidden":
            self.handle_set_hidden(data)
        elif t == "remote_cmd":
            self.handle_remote_cmd(data)
        elif t == "typing":
            self.handle_typing(data)
        elif t == "private_chat":
            self.handle_private_chat(data)
        elif t == "announcement":
            self.handle_announcement(data)
        elif t == "notify":
            self.handle_notify(data)
        elif t == "notify2":
            self.handle_notify2(data)
        elif t == "notify3":
            self.handle_notify3(data)
        elif t == "admin_action":
            self.handle_admin_action(data)
        else:
            self.send_error_msg("Unknown type: " + str(t))

    def on_close(self):
        if self.username:
            print(self.username, "disconnected")
            self.remove_user()

    def send(self, obj):
        obj["timestamp"] = time.time()
        try:
            self.write_message(json.dumps(obj))
        except Exception:
            pass

    def send_error_msg(self, msg, code=None, **extra):
        payload = {"type": "error", "message": msg}
        if code:
            payload["code"] = code
        payload.update(extra or {})
        self.send(payload)

    def add_user(self, username, hidden, user_id=None, is_admin=False, game_status=None, place_id=None, job_id=None, activity_hidden=False, display_name=""):
        connections[username] = self
        user_data[username] = {
            "hidden": hidden,
            "last_seen": time.time(),
            "user_id": user_id,
            "admin": bool(is_admin),
            "game_status": game_status or "",
            "place_id": place_id,
            "job_id": job_id,
            "activity_hidden": bool(activity_hidden),
            "display_name": display_name or "",
        }

    def remove_user(self):
        u = self.username
        if not u:
            return
        connections.pop(u, None)
        user_data.pop(u, None)

    def handle_register(self, data):
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

        if len(raw_game) > CONFIG["max_game_name_length"]:
            raw_game = raw_game[: CONFIG["max_game_name_length"]]

        if not user_id or user_id <= 0:
            self.send_error_msg("Missing/invalid userId")
            return

        rb_name, rb_display = fetch_roblox_user(user_id)
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
        if is_admin and ADMIN_SECRET:
            provided = data.get("adminKey")
            if not (isinstance(provided, str) and provided and provided == ADMIN_SECRET):
                is_admin = False

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
        )

        self.send({
            "type": "registered",
            "username": username,
            "displayName": display_name,
            "token": "dummy_token",
            "hidden": hidden,
            "userId": user_id,
            "admin": is_admin,
            "game": raw_game,
            "placeId": place_id,
            "jobId": job_id,
        })

        self.send({"type": "user_list", "users": get_user_list()})
        if is_admin:
            self.send({"type": "user_list_admin", "users": get_user_list_admin()})

    def handle_chat(self, data):
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
            remaining = int(max(0, mute_info["until"] - time.time()))
            msg = f"You are muted in NA Chat ({remaining}s left)"
            if mute_info["reason"]:
                msg += f" - {mute_info['reason']}"
            self.send_error_msg(msg, code="muted", until=mute_info["until"], reason=mute_info["reason"])
            return

        msg = (data.get("message") or "").strip()
        if not msg:
            self.send_error_msg("Message cannot be empty")
            return
        if len(msg) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
            return

        info = user_data.get(self.username, {})
        user_id = info.get("user_id")
        is_admin = bool(info.get("admin", False))
        game_status = info.get("game_status") or ""

        broadcast({
            "type": "chat",
            "username": self.username,
            "message": msg,
            "userId": user_id,
            "admin": is_admin,
            "game": game_status,
        })

    def handle_heartbeat(self):
        if not self.username:
            return
        d = user_data.get(self.username)
        if d:
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
        self.send({"type": "hidden_updated", "hidden": new_hidden})

    def handle_typing(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        if user_data.get(self.username, {}).get("hidden"):
            return
        is_typing = bool(data.get("is_typing", False))
        scope = data.get("scope") or "global"
        broadcast(
            {"type": "typing", "username": self.username, "is_typing": is_typing, "scope": scope},
            exclude=self.username,
        )

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

        message = (data.get("message") or "").strip()
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

        payload = {"type": "private_chat", "from": self.username, "to": target, "message": message}
        send_to_user(self.username, payload)
        if not send_to_user(target, payload):
            self.send_error_msg(f"User '{target}' is not online")

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
        payload = {
            "type": "remote_cmd",
            "fromUserId": info.get("user_id"),
            "fromUsername": self.username,
            "args": args,
            "target": target,
        }
        if target is None or target == "" or target == "all":
            broadcast(payload)
            return
        try:
            target_id = int(target)
        except (TypeError, ValueError):
            self.send_error_msg("Invalid target")
            return
        payload["timestamp"] = time.time()
        msg = json.dumps(payload)
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
        msg = json.dumps(payload)
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
        message = (data.get("message") or "").strip()
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        if len(message) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
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
        message = (data.get("message") or "").strip()
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        if len(message) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
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
        message = (data.get("message") or "").strip()
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        if len(message) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
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
        message = (data.get("message") or "").strip()
        if not message:
            self.send_error_msg("Message cannot be empty")
            return
        if len(message) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
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

        if action in ("kick", "ban", "unban", "mute", "unmute") and not target:
            self.send_error_msg("Missing target")
            return

        if target == self.username and action in ("ban", "kick"):
            self.send_error_msg("You cannot target yourself")
            return

        if action == "kick":
            ws = connections.get(target)
            if not ws:
                self.send_error_msg("Target not found")
            else:
                try:
                    ws.close(4000, "Kicked from NA Chat")
                except Exception:
                    pass
                broadcast({"type": "system", "message": f"{target} was kicked from NA Chat"})

        elif action == "ban":
            ban_user(target)
            ws = connections.get(target)
            if ws:
                try:
                    ws.close(4001, "Banned from NA Chat")
                except Exception:
                    pass
            broadcast({"type": "system", "message": f"{target} was banned from NA Chat"})

        elif action == "unban":
            unban_user(target)
            self.send({"type": "system", "message": f"{target} was unbanned from NA Chat"})

        elif action == "mute":
            if not duration:
                duration = 300
            reason = (data.get("reason") or "").strip()
            mute_user(target, duration, reason=reason)
            reason_suffix = f" - {reason}" if reason else ""
            broadcast({"type": "system", "message": f"{target} was muted in NA Chat ({int(duration)}s){reason_suffix}"})

        elif action == "unmute":
            unmute_user(target)
            broadcast({"type": "system", "message": f"{target} was unmuted in NA Chat"})

        elif action == "refresh":
            pass
        else:
            self.send_error_msg("Unknown admin action")
            return

        self.send({"type": "admin_state", "banned": get_ban_list(), "muted": get_mute_list()})


class HealthHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("OK")


def make_app():
    return tornado.web.Application(
        [
            (r"/swimhub/?", IntegrationHandler),
            (r"/healthz", HealthHandler),
        ]
    )


def cleanup_inactive_users():
    timeout = CONFIG["heartbeat_timeout"]
    now = time.time()
    to_remove = []
    for name, data in list(user_data.items()):
        last_seen = data.get("last_seen", now)
        if now - last_seen > timeout:
            to_remove.append(name)
    for name in to_remove:
        print("Removing inactive user", name)
        ws = connections.pop(name, None)
        user_data.pop(name, None)
        if ws:
            try:
                ws.close(1000, "Inactive timeout")
            except Exception:
                pass


if __name__ == "__main__":
    app = make_app()
    port = int(os.environ.get("PORT", "8000"))
    app.listen(port)
    tornado.ioloop.PeriodicCallback(cleanup_inactive_users, 10000).start()
    print("=" * 50)
    print(f"Server started on port {port} ..")
    print("=" * 50)
    tornado.ioloop.IOLoop.current().start()
