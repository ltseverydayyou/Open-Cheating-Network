local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")

local IntegrationService = {}
IntegrationService.__index = IntegrationService

IntegrationService.OnChatMessage = Instance.new("BindableEvent")
IntegrationService.OnSystemMessage = Instance.new("BindableEvent")
IntegrationService.OnUserListUpdate = Instance.new("BindableEvent")
IntegrationService.OnConnected = Instance.new("BindableEvent")
IntegrationService.OnDisconnected = Instance.new("BindableEvent")
IntegrationService.OnError = Instance.new("BindableEvent")

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
        --warn("[IntegrationService] Cannot send message: not connected")
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
    
    local success, data = pcall(function()
        return HttpService:JSONDecode(message)
    end)
    
    if not success then
        --warn("[IntegrationService] Failed to decode message:", message)
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
        
    elseif msg_type == "heartbeat_ack" then
        
    elseif msg_type == "hidden_updated" then
        is_hidden = data.hidden
        IntegrationService.OnSystemMessage:Fire(
            is_hidden and "You are now hidden" or "You are now visible", 
            data.timestamp
        )
        
    elseif msg_type == "error" then
        --warn("[IntegrationService] Server error:", data.message)
        IntegrationService.OnError:Fire(data.message, data.timestamp)
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
        --warn("[IntegrationService] Already connected")
        return false
    end
    
    local success, connection = pcall(function()
        return WebSocket.connect(config.serverUrl)
    end)
    
    if not success then
        --warn("[IntegrationService] Failed to connect:", connection)
        return false
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
        return false
    end
    
    username = player.Name --nameChecker(player)
    local userId = player.UserId

    local gameStatus = nil
    local placeIdForShare = nil
    local jobIdForShare = nil
    local okGameFlag, flag = pcall(function()
        local na = rawget(_G, "NAmanage")
        return na and na.NAChatGameActivityEnabled and na.NAChatGameActivityEnabled()
    end)
    if not (okGameFlag and flag == false) then
        gameStatus = getGameStatus()
        placeIdForShare = game.PlaceId
        jobIdForShare = tostring(game.JobId)
    else
        gameStatus = "Game: Hidden"
    end
    
    if not connect() then
        return false
    end
    
    task.wait(0.5)
    
    if not send_message("register", {
        username = username,
        userId = userId,
        game = gameStatus,
        placeId = placeIdForShare,
        jobId = jobIdForShare,
        hidden = config.hidden
    }) then
        --warn("[IntegrationService] Failed to send registration")
        return false
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
        --warn("[IntegrationService] Cannot send message: not registered")
        return false
    end
    
    if is_hidden then
        --warn("[IntegrationService] Cannot send message: hidden mode active")
        return false
    end
    
    if type(message) ~= "string" then
        message = tostring(message)
    end
    
    message = message:gsub("^%s+", ""):gsub("%s+$", "")
    
    if message == "" then
        --warn("[IntegrationService] Cannot send empty message")
        return false
    end
    
    return send_message("chat", {message = message})
end

function IntegrationService.GetUsers()
    if not registered then
        --warn("[IntegrationService] Cannot get users: not registered")
        return false
    end
    
    if is_hidden then
        --warn("[IntegrationService] Cannot get users: hidden mode active")
        return false
    end
    
    return send_message("get_users")
end

function IntegrationService.IsConnected()
    return registered and ws ~= nil
end

function IntegrationService.IsHidden()
    return is_hidden
end

function IntegrationService.SetHidden(hidden)
    if not registered then
        --warn("[IntegrationService] Cannot change hidden status: not registered")
        return false
    end
    
    if type(hidden) ~= "boolean" then
        --warn("[IntegrationService] Hidden must be a boolean value")
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
        ws:Close()
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

return IntegrationService
