import os
import json
import time
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
    11761417,    # Main
    530829101,   # Viper
    817571515,   # Aimlock
    1844177730,  # glexinator
    2624269701,  # Akim
    2502806181,  # Main Alt
    1594235217,  # Purple
    2845101018,  # alt
    2019160453,  # grim
}

connections = {}      # username -> WebSocketHandler
user_data = {}        # username -> {hidden, last_seen}
ip_users = {}         # ip -> set(usernames)


def get_user_list():
    result = []
    for u, d in user_data.items():
        if d.get("hidden", False):
            continue
        result.append({
            "username": u,
            "userId": d.get("user_id"),
            "admin": bool(d.get("admin", False)),
            "game": d.get("game_status") or "",
            "placeId": d.get("place_id"),
            "jobId": d.get("job_id"),
        })
    return result


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
        elif t == "set_hidden":
            self.handle_set_hidden(data)
        else:
            self.send_error_msg("Unknown type: " + str(t))

    def on_close(self):
        if self.username:
            print(self.username, "disconnected")
            hidden = user_data.get(self.username, {}).get("hidden", False)
            self.remove_user()
            # if not hidden:
            #     broadcast({"type": "system", "message": f"{self.username} left the chat"})

    # ---------- helpers ----------

    def send(self, obj):
        obj["timestamp"] = time.time()
        try:
            self.write_message(json.dumps(obj))
        except Exception:
            pass

    def send_error_msg(self, msg):
        self.send({"type": "error", "message": msg})

    def add_user(self, username, hidden, user_id=None, is_admin=False, game_status=None, place_id=None, job_id=None):
        connections[username] = self
        user_data[username] = {
            "hidden": hidden,
            "last_seen": time.time(),
            "user_id": user_id,
            "admin": bool(is_admin),
            "game_status": game_status or "",
            "place_id": place_id,
            "job_id": job_id,
        }

    def remove_user(self):
        u = self.username
        if not u:
            return
        connections.pop(u, None)
        user_data.pop(u, None)

    # ---------- handlers ----------

    def handle_register(self, data):
        username = (data.get("username") or "").strip()
        hidden = bool(data.get("hidden", False))
        user_id = data.get("userId")
        raw_game = (data.get("game") or "").strip()
        place_id = data.get("placeId")
        job_id = data.get("jobId")

        if len(raw_game) > CONFIG["max_game_name_length"]:
            raw_game = raw_game[: CONFIG["max_game_name_length"]]

        if not username or len(username) > CONFIG["max_username_length"]:
            self.send_error_msg(
                f"Username must be 1-{CONFIG['max_username_length']} characters"
            )
            return

        cleaned = (
            username
            .replace("_", "")
            .replace(" ", "")
            .replace("(", "")
            .replace(")", "")
            .replace("@", "")
        )

        if not cleaned.isalnum():
            self.send_error_msg(
                "Username contains invalid characters (only letters, numbers, _, space, @, ( ) allowed)"
            )
            return

        if username in connections and connections[username] is not self:
            try:
                connections[username].close(1000, "Replaced")
            except Exception:
                pass

        # authoritative admin check, server-side only
        is_admin = False
        try:
            if isinstance(user_id, int) and user_id in ADMIN_IDS:
                is_admin = True
        except Exception:
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
        )

        self.send({
            "type": "registered",
            "username": username,
            "token": "dummy_token",
            "hidden": hidden,
            "userId": user_id,
            "admin": is_admin,
            "game": raw_game,
            "placeId": place_id,
            "jobId": job_id,
        })

        # if not hidden:
        #     broadcast(
        #         {"type": "system", "message": f"{username} joined the chat"},
        #         exclude=username,
        #     )

        self.send({"type": "user_list", "users": get_user_list()})

    def handle_chat(self, data):
        if not self.username:
            self.send_error_msg("Not registered")
            return
        if user_data.get(self.username, {}).get("hidden"):
            self.send_error_msg("Hidden users cannot send messages")
            return

        msg = (data.get("message") or "").strip()
        if not msg:
            self.send_error_msg("Message cannot be empty")
            return
        if len(msg) > CONFIG["max_message_length"]:
            self.send_error_msg("Message too long")
            return

        print("chat from", self.username, ":", msg)

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

        # if new_hidden:
        #     broadcast({"type": "system", "message": f"{self.username} left the chat"})
        # else:
        #     broadcast(
        #         {"type": "system", "message": f"{self.username} joined the chat"},
        #         exclude=self.username,
        #     )
        #     self.send({"type": "user_list", "users": get_user_list()})


class HealthHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("OK")


def make_app():
    return tornado.web.Application(
        [
            (r"/swimhub", IntegrationHandler),
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

        # broadcast({
        #     "type": "system",
        #     "message": f"{name} left (timeout)"
        # })


if __name__ == "__main__":
    app = make_app()

    port = int(os.environ.get("PORT", "8000"))
    app.listen(port)

    tornado.ioloop.PeriodicCallback(cleanup_inactive_users, 10000).start()

    print("=" * 50)
    print(f"Server started on port {port} ..")
    print("=" * 50)

    tornado.ioloop.IOLoop.current().start()
