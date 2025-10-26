--!native
--!strict
--!optimize 2

--[=[
	This module handles serializing a Roblox KeyframeSequence into a table format
	that can be JSON encoded and sent to the Blender addon.
]=]

local Components = script.Parent
local BaseXX = require(Components.BaseXX)
local DeflateLua = require(Components.DeflateLua)

local AnimationSerializer = {}
AnimationSerializer.__index = AnimationSerializer

export type SerializedAnimation = {
	t: number,
	kfs: { { t: number, kf: { [string]: { components: { number }, easingStyle: string, easingDirection: string } } } },
	is_deform_bone_rig: boolean,
}

type RigPart = {
	isDeformBone: boolean,
	poses: { [number]: { CFrame: CFrame, EasingStyle: string, EasingDirection: string } },
}

type RigType = {
	isDeformRig: boolean,
	bones: { [string]: RigPart },
	ToRobloxAnimation: (self: RigType) -> KeyframeSequence,
}

type self = {}

function AnimationSerializer.new()
	local self: self = {}
	return setmetatable(self, AnimationSerializer)
end

function AnimationSerializer:serialize(keyframeSequence: KeyframeSequence, rig: RigType): SerializedAnimation?
	local allKeyframes = keyframeSequence:GetKeyframes()

	if #allKeyframes == 0 then
		warn("Animation has no keyframes.")
		return nil
	end

	-- Pre-allocate collected table with estimated size
	local estimatedSize = math.min(#allKeyframes, 1000) -- Cap estimation to avoid huge allocations
	local collected = table.create(estimatedSize)
	local maxTime = 0
	local startTime = (allKeyframes[1] :: any).Time

	-- Sort in-place for better performance
	table.sort(allKeyframes, function(a, b)
		return (a :: any).Time < (b :: any).Time
	end)

	-- Pre-cache common values to avoid repeated property access
	local isDeformRig = rig.isDeformRig
	local collectedCount = 0

	for i = 1, #allKeyframes do
		local kf = allKeyframes[i]
		if kf:IsA("Keyframe") then
			-- Pre-allocate state table as a proper dictionary
			local state = {}
			
			-- Get descendants once and cache the result
			local descendants = kf:GetDescendants()
			for j = 1, #descendants do
				local pose = descendants[j]
				if pose:IsA("Pose") then
					local weight = (pose :: any).Weight
					if type(weight) == "number" and weight > 0 then
						state[pose.Name] = {
							components = { pose.CFrame:GetComponents() },
							easingStyle = pose.EasingStyle.Name,
							easingDirection = pose.EasingDirection.Name,
						}
					end
				end
			end
			
			-- Only add keyframe if it has poses
			if next(state) then
				collectedCount += 1
				collected[collectedCount] = { 
					t = (kf :: any).Time - startTime, 
					kf = state 
				}
			end
		end
	end

	if collectedCount == 0 then
		warn("Animation has no poses to serialize.")
		return nil
	end

	-- Trim collected table to actual size
	if collectedCount < #collected then
		for i = collectedCount + 1, #collected do
			collected[i] = nil
		end
	end

	if #allKeyframes > 0 then
		maxTime = (allKeyframes[#allKeyframes] :: any).Time - startTime
	end

	local result: SerializedAnimation = {
		t = maxTime,
		kfs = collected,
		is_deform_bone_rig = isDeformRig,
	}

	return result
end

function AnimationSerializer:serializeFromRig(rig: RigType): SerializedAnimation?
	local keyframeSequence = rig:ToRobloxAnimation()
	if not keyframeSequence then
		return nil
	end
	return self:serialize(keyframeSequence, rig)
end

function AnimationSerializer:deserialize(data: string, isBinary: boolean): any?
	-- Cache HttpService to avoid repeated service lookups
	local httpService = game:GetService("HttpService")
	
	-- Try direct JSON parsing first (fastest path for uncompressed data)
	if not isBinary then
		local okJson, jsonResult = pcall(function()
			return httpService:JSONDecode(data)
		end)
		if okJson then
			return jsonResult
		end
	end

	-- Pre-allocate buffer with better size estimation
	local bufferSize = if isBinary then #data else math.floor(#data * 0.75)
	local buffer = table.create(bufferSize)
	if not buffer then
		warn("Failed to create buffer")
		return nil
	end
	local bufferIndex = 1

	-- Optimized byte collection function
	local function collectByte(byte: number)
		buffer[bufferIndex] = string.char(byte)
		bufferIndex += 1
	end

    -- Decompress the data
    local success, decompressError = pcall(function()
		if isBinary then
			-- Direct binary path
			DeflateLua.inflate_zlib({
				disable_crc = true,
				input = data :: any,
				output = collectByte :: any,
			})
        else
			-- Legacy base64 path - optimize string cleaning
            local clean = string.gsub(data, "%s", "") -- More efficient pattern
            local decoded = BaseXX.from_base64(clean) :: any
			DeflateLua.inflate_zlib({
				disable_crc = true,
				input = decoded :: any,
				output = collectByte :: any,
			})
		end
		return true
	end)

	if not success then
        warn("Decompression failed: " .. tostring(decompressError))
        -- Optimized fallbacks
        if not isBinary then
            -- Try base64 decode to JSON
            local clean = string.gsub(data, "%s", "")
            local okB64, decodedOrErr = pcall(function()
                return BaseXX.from_base64(clean)
            end)
            if okB64 and type(decodedOrErr) == "string" and #decodedOrErr > 0 then
                local okJson, jsonTbl = pcall(function()
                    return httpService:JSONDecode(decodedOrErr)
                end)
                if okJson then
                    return jsonTbl
                end
            end
        else
            -- Binary path: try direct JSON parsing
            local okJson, jsonTbl = pcall(function()
                return httpService:JSONDecode(data)
            end)
            if okJson then
                return jsonTbl
            end
        end
        return nil
	end

	-- Use table.concat with explicit length for better performance
	local jsonStr = table.concat(buffer, "", 1, bufferIndex - 1)

	-- Clear buffer to help GC
	table.clear(buffer)

    -- Parse the JSON
    local jsonSuccess, jsonResult = pcall(function()
        return httpService:JSONDecode(jsonStr)
    end)

    if not jsonSuccess then
        warn("JSON parsing failed: " .. tostring(jsonResult))
        return nil
    end

    return jsonResult
end

return AnimationSerializer
