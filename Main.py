import os
import json
import time
import tornado.ioloop
import tornado.web
import tornado.websocket

CONFIG = {
    "max_username_length": 20,
    "max_message_length": 500,
    "heartbeat_timeout": 30,
}

connections = {}      # username -> WebSocketHandler
user_data = {}        # username -> {hidden, last_seen}
ip_users = {}         # ip -> set(usernames)


def get_user_list():
    return [u for u, d in user_data.items() if not d.get("hidden", False)]


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
            if not hidden:
                broadcast({"type": "system", "message": f"{self.username} left the server"})

    # ---------- helpers ----------

    def send(self, obj):
        obj["timestamp"] = time.time()
        try:
            self.write_message(json.dumps(obj))
        except Exception:
            pass

    def send_error_msg(self, msg):
        self.send({"type": "error", "message": msg})

    def add_user(self, username, hidden):
        connections[username] = self
        user_data[username] = {
            "hidden": hidden,
            "last_seen": time.time(),
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

        if not username or len(username) > CONFIG["max_username_length"]:
            self.send_error_msg(
                f"Username must be 1-{CONFIG['max_username_length']} characters"
            )
            return

        if not username.replace("_", "").isalnum():
            self.send_error_msg("Username must be alphanumeric (underscore allowed)")
            return

        if username in connections and connections[username] is not self:
            # kick old connection
            try:
                connections[username].close(1000, "Replaced")
            except Exception:
                pass

        self.username = username
        self.add_user(username, hidden)

        self.send({
            "type": "registered",
            "username": username,
            "token": "dummy_token",
            "hidden": hidden,
        })

        if not hidden:
            broadcast(
                {"type": "system", "message": f"{username} joined the server"},
                exclude=username,
            )

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

        broadcast({
            "type": "chat",
            "username": self.username,
            "message": msg,
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

        if new_hidden:
            broadcast({"type": "system", "message": f"{self.username} left the server"})
        else:
            broadcast(
                {"type": "system", "message": f"{self.username} joined the server"},
                exclude=self.username,
            )
            self.send({"type": "user_list", "users": get_user_list()})


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


if __name__ == "__main__":
    app = make_app()

    port = int(os.environ.get("PORT", "8000")) 
    app.listen(port)
    
    tornado.ioloop.PeriodicCallback(cleanup_inactive_users, 10000).start()
    
    print("="*50)
    print(f"Server started on port {port} ..")
    if CONFIG["bridge_servers"]:
        print(f"Bridging to {len(CONFIG['bridge_servers'])} servers")
    print("="*50)
    
    tornado.ioloop.IOLoop.current().start()
