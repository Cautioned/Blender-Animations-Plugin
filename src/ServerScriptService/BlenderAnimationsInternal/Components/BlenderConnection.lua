--!native
--!strict
--!optimize 2

--[=[
	This module handles all direct HTTP communication with the Blender addon server.
	It is designed to be a stateless service, with dependencies like HttpService
	and the server port passed in during construction or on each method call.
]=]

type HttpMethod = "CONNECT" | "DELETE" | "GET" | "HEAD" | "OPTIONS" | "PATCH" | "POST" | "PUT" | "TRACE"

local BlenderConnection = {}
BlenderConnection.__index = BlenderConnection

type self = {
	HttpService: HttpService,
}

function BlenderConnection.new(httpService: HttpService)
	local self: self = {
		HttpService = httpService,
	}
	return setmetatable(self, BlenderConnection)
end

function BlenderConnection:ListArmatures(port: number)
	if type(port) ~= "number" or port <= 0 then
		warn("Invalid port for ListArmatures")
		return nil
	end

	local success, response = pcall(function()
		local url = string.format("http://localhost:%d/list_armatures", port)
		return self.HttpService:RequestAsync({
			Url = url,
			Method = "GET" :: HttpMethod,
			Compress = Enum.HttpCompression.None,
		})
	end)

	if not success or not response or not response.Success then
		warn("Failed to get armatures:", response and response.StatusMessage or response)
		return nil
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)

	if not decodeSuccess then
		warn("Failed to decode armature list:", data)
		return nil
	end

	return data.armatures
end

function BlenderConnection:ImportAnimation(port: number, armatureName: string)
	if type(port) ~= "number" or port <= 0 then
		warn("Invalid port for ImportAnimation")
		return nil
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		warn("Invalid armature name for ImportAnimation")
		return nil
	end

	local success, response = pcall(function()
		local url = string.format("http://localhost:%d/export_animation/%s", port, armatureName)
		return self.HttpService:RequestAsync({
			Url = url,
			Method = "GET" :: HttpMethod,
			Body = nil,
			Headers = {
				["Accept"] = "application/octet-stream",
			},
			Compress = Enum.HttpCompression.None,
		})
	end)

	if success and response and response.Success then
		return response.Body
	else
		local errorMsg = "Failed to import animation"
		if response and not response.Success then
			errorMsg = errorMsg .. ": " .. (response.StatusMessage or "Unknown Error")
		elseif not success then
			errorMsg = errorMsg .. ": " .. tostring(response)
		end
		warn(errorMsg)
		return nil
	end
end

function BlenderConnection:ExportAnimation(port: number, animationData: any, targetArmature: string?)
	if type(port) ~= "number" or port <= 0 then
		warn("Invalid port for ExportAnimation")
		return false
	end

	local encoded = nil
	local okEncode, encodeErr = pcall(function()
		encoded = self.HttpService:JSONEncode(animationData)
	end)
	if not okEncode or not encoded then
		warn("Failed to encode animation data for export: " .. tostring(encodeErr))
		return false
	end

	local success, response = pcall(function()
		local url = string.format("http://localhost:%d/import_animation", port)
		if targetArmature then
			url = url .. "?armature=" .. self.HttpService:UrlEncode(targetArmature)
		end
		return self.HttpService:RequestAsync({
			Url = url,
			Method = "POST" :: HttpMethod,
			Headers = {
				["Content-Type"] = "application/octet-stream",
			},
			Body = encoded,
			Compress = Enum.HttpCompression.None, -- Disable compression for faster local transfers
		})
	end)

	if success and response and response.Success then
		print("Successfully exported animation to Blender.")
		return true
	else
		local errorMsg = "Failed to export animation to Blender"
		if response and not response.Success then
			errorMsg = errorMsg .. ": " .. (response.StatusMessage or "Unknown Error")
		elseif not success then
			errorMsg = errorMsg .. ": " .. tostring(response)
		end
		warn(errorMsg)
		return false
	end
end

function BlenderConnection:CheckAnimationStatus(port: number, armatureName: string, lastKnownHash: string)
	if type(port) ~= "number" or port <= 0 then
		return nil
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		return nil
	end

	local success, response = pcall(function()
		local url = string.format(
			"http://localhost:%d/animation_status?armature=%s&last_known_hash=%s",
			port,
			self.HttpService:UrlEncode(armatureName),
			lastKnownHash or ""
		)
		return self.HttpService:RequestAsync({
			Url = url,
			Method = "GET" :: HttpMethod,
			Compress = Enum.HttpCompression.None,
		})
	end)

	if not success or not response or not response.Success then
		return nil
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)

	if not decodeSuccess then
		return nil
	end

	return data
end

function BlenderConnection:GetBoneRest(port: number, armatureName: string)
	if type(port) ~= "number" or port <= 0 then
		warn("Invalid port for GetBoneRest")
		return nil
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		warn("Invalid armature name for GetBoneRest")
		return nil
	end

	local success, response = pcall(function()
		local url = string.format("http://localhost:%d/get_bone_rest/%s", port, self.HttpService:UrlEncode(armatureName))
		return self.HttpService:GetAsync(url)
	end)

	if not success then
		warn("Failed to get bone rest poses:", response)
		return nil
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response)
	end)

	if not decodeSuccess then
		warn("Failed to decode bone rest data:", data)
		return nil
	end

	return data
end

return BlenderConnection
