local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")

local IntegrationService = {}
IntegrationService.__index = IntegrationService

IntegrationService.OnChatMessage = Instance.new("BindableEvent")
IntegrationService.OnSystemMessage = Instance.new("BindableEvent")
IntegrationService.OnUserListUpdate = Instance.new("BindableEvent")
IntegrationService.OnUserListUpdateAdmin = Instance.new("BindableEvent")
IntegrationService.OnConnected = Instance.new("BindableEvent")
IntegrationService.OnDisconnected = Instance.new("BindableEvent")
IntegrationService.OnError = Instance.new("BindableEvent")
IntegrationService.OnRemoteCommand = Instance.new("BindableEvent")

IntegrationService.OnTyping = Instance.new("BindableEvent")
IntegrationService.OnPrivateMessage = Instance.new("BindableEvent")
IntegrationService.OnAnnouncement = Instance.new("BindableEvent")
IntegrationService.OnNotify = Instance.new("BindableEvent")
IntegrationService.OnNotify2 = Instance.new("BindableEvent")
IntegrationService.OnNotify3 = Instance.new("BindableEvent")
IntegrationService.OnAdminState = Instance.new("BindableEvent")

local ws = nil
local registered = false
local username = nil
local token = nil
local reconnecting = false
local heartbeat_thread = nil
local is_hidden = false
local config = {
    serverUrl = "wss://witty-minette-adonis-632b17c0.koyeb.app/swimhub",
    heartbeatInterval = 5,
    reconnectDelay = 3,
    autoReconnect = true,
    hidden = false
}

local lastDecodeWarnText = nil
local lastDecodeWarnTime = 0

local function getEnv()
    local ok, env = pcall(function()
        local g = getgenv
        if type(g) == "function" then
            return g()
        end
    end)

    if ok and type(env) == "table" then
        return env
    end

    return _G
end

local resolvedWebSocketConnect = nil

local function resolveWebSocketConnect()
    if resolvedWebSocketConnect ~= nil then
        return resolvedWebSocketConnect
    end

    local env = getEnv()

    local function tryContainer(container)
        if type(container) == "table" then
            if type(container.connect) == "function" then
                resolvedWebSocketConnect = container.connect
            elseif type(container.Connect) == "function" then
                resolvedWebSocketConnect = container.Connect
            end
        elseif type(container) == "function" and resolvedWebSocketConnect == nil then
            resolvedWebSocketConnect = container
        end
    end

    tryContainer(env.WebSocket or (_G and _G.WebSocket))
    tryContainer(env.websocket or (_G and _G.websocket))

    if type(env.syn) == "table" then
        tryContainer(env.syn.websocket or env.syn.WebSocket)
    end

    if type(env.http) == "table" then
        tryContainer(env.http.websocket or env.http.WebSocket)
    end

    if type(env.solara) == "table" then
        tryContainer(env.solara.websocket or env.solara.WebSocket)
    end

    return resolvedWebSocketConnect
end

local function getGameStatus()
    local ok, result = pcall(function()
        local placeId = game.PlaceId
        local info = game:GetService("MarketplaceService"):GetProductInfo(placeId)
        local name = typeof(info) == "table" and info.Name or nil
        return name or ("PlaceId: " .. tostring(placeId))
    end)
    if ok and type(result) == "string" then
        return result
    end
    return "Unknown place"
end

local function send_message(msg_type, data)
    if not ws then 
        warn("[IntegrationService] Cannot send message: not connected")
        return false
    end
    
    local message = {
        type = msg_type,
        timestamp = os.time()
    }
    
    if data then
        for k, v in pairs(data) do
            message[k] = v
        end
    end
    
    local success, json = pcall(function()
        return HttpService:JSONEncode(message)
    end)
    
    if not success then
        warn("[IntegrationService] Failed to encode message:", tostring(json))
        return false
    end
    
    local send_success = pcall(function()
        ws:Send(json)
    end)
    
    if not send_success then
        return false
    end
    
    return true
end

local function isProperty(inst, prop)
	local s, r = pcall(function() return inst[prop] end)
	if not s then return nil end
	return r
end

local function setProperty(inst, prop, v)
	local s, _ = pcall(function() inst[prop] = v end)
	return s
end

local function nameChecker(p)
	if not isProperty(p, "DisplayName") then
		return p.Name
	end

	local displayName = p.DisplayName
	if displayName:lower() == p.Name:lower() then
		return '@'..p.Name
	else
		return displayName..' (@'..p.Name..')'
	end
end

local function handle_message(message)
    if typeof(message) == "table" and message.data then
        message = message.data
    end

    if type(message) ~= "string" then
        return
    end

    local trimmed = message:match("^%s*(.-)%s*$")
    if trimmed == "" then
        return
    end

    local firstChar = trimmed:sub(1, 1)
    if firstChar ~= "{" and firstChar ~= "[" then
        return
    end

    local success, data = pcall(function()
        return HttpService:JSONDecode(trimmed)
    end)
    
    if not success then
        local now = os.clock()
        if (now - (lastDecodeWarnTime or 0)) > 10 or trimmed ~= lastDecodeWarnText then
            lastDecodeWarnText = trimmed
            lastDecodeWarnTime = now
            warn("[IntegrationService] Failed to decode message:", trimmed)
        end
        return
    end
    
    local msg_type = data.type
    
    if msg_type == "registered" then
        registered = true
        username = data.username
        token = data.token
        is_hidden = data.hidden or false
        
        IntegrationService.OnConnected:Fire(username, token, is_hidden, data.userId, data.admin, data.game)
        
    elseif msg_type == "chat" then
        IntegrationService.OnChatMessage:Fire(data.username, data.message, data.timestamp, data.userId, data.admin, data.game)
        
    elseif msg_type == "system" then
        IntegrationService.OnSystemMessage:Fire(data.message, data.timestamp)
        
    elseif msg_type == "user_list" then
        IntegrationService.OnUserListUpdate:Fire(data.users, data.timestamp)

    elseif msg_type == "user_list_admin" then
        IntegrationService.OnUserListUpdateAdmin:Fire(data.users, data.timestamp)
        
    elseif msg_type == "heartbeat_ack" then
        -- ignore, used only to keep connection alive
    elseif msg_type == "hidden_updated" then
        is_hidden = data.hidden
        IntegrationService.OnSystemMessage:Fire(
            is_hidden and "You are now hidden" or "You are now visible", 
            data.timestamp
        )

    elseif msg_type == "remote_cmd" then
        IntegrationService.OnRemoteCommand:Fire(
            data.fromUserId,
            data.fromUsername,
            data.args,
            data.target
        )

    elseif msg_type == "typing" then
        IntegrationService.OnTyping:Fire(
            data.username,
            data.is_typing == true,
            data.scope or "global",
            data.timestamp
        )

    elseif msg_type == "private_chat" then
        IntegrationService.OnPrivateMessage:Fire(
            data.from,
            data.to,
            data.message,
            data.timestamp
        )

    elseif msg_type == "announcement" then
        IntegrationService.OnAnnouncement:Fire(
            data.from,
            data.message,
            data.timestamp
        )

    elseif msg_type == "notify" then
        IntegrationService.OnNotify:Fire(
            data.from,
            data.message,
            data.duration,
            data.timestamp
        )

    elseif msg_type == "notify2" then
        IntegrationService.OnNotify2:Fire(
            data.from,
            data.message,
            data.timestamp
        )

    elseif msg_type == "notify3" then
        IntegrationService.OnNotify3:Fire(
            data.from,
            data.message,
            data.timestamp
        )

    elseif msg_type == "admin_state" then
        IntegrationService.OnAdminState:Fire(data)

    elseif msg_type == "error" then
        warn("[IntegrationService] Server error:", data.message)
        IntegrationService.OnError:Fire(data.message, data.timestamp, data)
    end
end

local function start_heartbeat()
    if heartbeat_thread then
        task.cancel(heartbeat_thread)
    end
    
    heartbeat_thread = task.spawn(function()
        while registered and ws do
            task.wait(config.heartbeatInterval)
            if registered then
                send_message("heartbeat")
            end
        end
    end)
end

local function stop_heartbeat()
    if heartbeat_thread then
        task.cancel(heartbeat_thread)
        heartbeat_thread = nil
    end
end

local function connect()
    if ws then
        warn("[IntegrationService] Already connected")
        return false, "already_connected"
    end
    
    local connector = resolveWebSocketConnect()
    if type(connector) ~= "function" then
        warn("[IntegrationService] WebSocket API not available in this executor")
        return false, "websocket_not_available"
    end

    local success, connection = pcall(connector, config.serverUrl)
    
    if not success then
        warn("[IntegrationService] Failed to connect:", connection)
        return false, tostring(connection or "connect_failed")
    end
    
    ws = connection
    
    ws.OnMessage:Connect(handle_message)
    
    ws.OnClose:Connect(function()
        registered = false
        stop_heartbeat()
        
        IntegrationService.OnDisconnected:Fire()
        
        ws = nil
        
        if config.autoReconnect and not reconnecting then
            reconnecting = true
            task.wait(config.reconnectDelay)
            reconnecting = false
            IntegrationService.Init()
        end
    end)
    
    return true
end

function IntegrationService.Init(custom_config)
    if custom_config then
        for k, v in pairs(custom_config) do
            config[k] = v
        end
    end
    
    local player = Players.LocalPlayer
    if not player then
        return false, "no_local_player"
    end
    
    username = player.Name --nameChecker(player)
    local userId = player.UserId

    local gameStatus = getGameStatus()
    local placeIdForShare = game.PlaceId
    local jobIdForShare = tostring(game.JobId)

    local activityHidden = false
    local okGameFlag, flag = pcall(function()
        return _G.NAChatGameActivityEnabled and _G.NAChatGameActivityEnabled()
    end)
    if okGameFlag and flag == false then
        activityHidden = true
    end
    
    local okConnect, connectErr = connect()
    if not okConnect then
        return false, connectErr or "connect_failed"
    end
    
    task.wait(0.5)
    
    if not send_message("register", {
        username = username,
        userId = userId,
        game = gameStatus,
        placeId = placeIdForShare,
        jobId = jobIdForShare,
        hidden = config.hidden,
        activityHidden = activityHidden,
    }) then
        warn("[IntegrationService] Failed to send registration")
        return false, "register_failed"
    end
    
    task.wait(1)
    if registered then
        start_heartbeat()
    end
    
    game:GetService("Players").LocalPlayer.AncestryChanged:Connect(function(_, parent)
        if not parent then
            IntegrationService.Disconnect()
        end
    end)

    return true
end

function IntegrationService.SendMessage(message)
    if not registered then
        warn("[IntegrationService] Cannot send message: not registered")
        return false
    end
    
    if is_hidden then
        warn("[IntegrationService] Cannot send message: hidden mode active")
        return false
    end
    
    if type(message) ~= "string" then
        message = tostring(message)
    end
    
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    
    if message == "" then
        warn("[IntegrationService] Cannot send empty message")
        return false
    end
    
    return send_message("chat", {message = message})
end

function IntegrationService.SendTyping(isTyping)
    if not registered then
        return false
    end
    if is_hidden then
        return false
    end
    return send_message("typing", {
        is_typing = isTyping and true or false,
        scope = "global",
    })
end

function IntegrationService.SendPrivateMessage(target, message)
    if not registered then
        return false
    end
    if is_hidden then
        return false
    end

    if type(target) ~= "string" or target == "" then
        return false
    end

    if type(message) ~= "string" then
        message = tostring(message)
    end
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    if message == "" then
        return false
    end

    return send_message("private_chat", {
        target = target,
        message = message,
    })
end

function IntegrationService.SendAnnouncement(message)
    if not registered then
        return false
    end

    if type(message) ~= "string" then
        message = tostring(message)
    end
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    if message == "" then
        return false
    end

    return send_message("announcement", {
        message = message,
    })
end

function IntegrationService.SendNotify(target, message, duration)
    if not registered or not ws then
        return false
    end

    if type(message) ~= "string" then
        message = tostring(message)
    end
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    if message == "" then
        return false
    end

    local dur = tonumber(duration) or 5

    local t
    if target == nil or target == "" or target == "all" then
        t = "all"
    else
        local n = tonumber(target)
        if n then
            t = n
        else
            t = tostring(target)
        end
    end

    return send_message("notify", {
        target = t,
        message = message,
        duration = dur,
    })
end

function IntegrationService.SendNotify2(target, message)
    if not registered or not ws then
        return false
    end

    if type(message) ~= "string" then
        message = tostring(message)
    end
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    if message == "" then
        return false
    end

    local t
    if target == nil or target == "" or target == "all" then
        t = "all"
    else
        local n = tonumber(target)
        if n then
            t = n
        else
            t = tostring(target)
        end
    end

    return send_message("notify2", {
        target = t,
        message = message,
    })
end

function IntegrationService.SendNotify3(target, message)
    if not registered or not ws then
        return false
    end

    if type(message) ~= "string" then
        message = tostring(message)
    end
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    if message == "" then
        return false
    end

    local t
    if target == nil or target == "" or target == "all" then
        t = "all"
    else
        local n = tonumber(target)
        if n then
            t = n
        else
            t = tostring(target)
        end
    end

    return send_message("notify3", {
        target = t,
        message = message,
    })
end

function IntegrationService.GetUsers()
    if not registered then
        warn("[IntegrationService] Cannot get users: not registered")
        return false
    end
    
    if is_hidden then
        warn("[IntegrationService] Cannot get users: hidden mode active")
        return false
    end
    
    return send_message("get_users")
end

function IntegrationService.GetUsersAdmin()
    if not registered then
        return false
    end

    if is_hidden then
        return false
    end

    return send_message("get_users_admin")
end

function IntegrationService.IsConnected()
    return registered and ws ~= nil
end

function IntegrationService.IsHidden()
    return is_hidden
end

function IntegrationService.SetHidden(hidden)
    if not registered then
        warn("[IntegrationService] Cannot change hidden status: not registered")
        return false
    end
    
    if type(hidden) ~= "boolean" then
        warn("[IntegrationService] Hidden must be a boolean value")
        return false
    end
    
    return send_message("set_hidden", {hidden = hidden})
end

function IntegrationService.GetUsername()
    return username
end

function IntegrationService.GetToken()
    return token
end

function IntegrationService.Disconnect()
    if ws then
        stop_heartbeat()

        pcall(function()
            if ws.Close then
                ws:Close()
            elseif ws.close then
                ws:close()
            elseif ws.Disconnect then
                ws:Disconnect()
            elseif ws.disconnect then
                ws:disconnect()
            end
        end)

        ws = nil
        registered = false
        username = nil
        token = nil
        is_hidden = false
    end
end

function IntegrationService.SetConfig(new_config)
    for k, v in pairs(new_config) do
        config[k] = v
    end
end

function IntegrationService.GetConfig()
    return config
end

function IntegrationService.SendRemoteCommand(target, args)
    if not registered or not ws then
        return false
    end
    if type(args) ~= "table" or #args == 0 then
        return false
    end

    local clean = {}
    for i, v in ipairs(args) do
        clean[i] = tostring(v)
    end

    local t
    if target == nil or target == "" or target == "all" then
        t = "all"
    else
        local n = tonumber(target)
        if n then
            t = n
        else
            t = tostring(target)
        end
    end

    return send_message("remote_cmd", {
        target = t,
        args = clean,
    })
end

function IntegrationService.SendAdminAction(action, target, duration, reason)
    if not registered or not ws then
        return false
    end

    action = tostring(action or ""):gsub("^%s+", ""):gsub("%s+$", "")
    target = tostring(target or ""):gsub("^%s+", ""):gsub("%s+$", "")
    reason = tostring(reason or ""):gsub("^%s+", ""):gsub("%s+$", "")

    if action == "" then
        return false
    end

    local payload = {
        action = action,
        target = target,
    }

    if duration ~= nil then
        local n = tonumber(duration)
        if n and n > 0 then
            payload.duration = n
        end
    end

    if reason ~= "" then
        payload.reason = reason
    end

    return send_message("admin_action", payload)
end

return IntegrationService
