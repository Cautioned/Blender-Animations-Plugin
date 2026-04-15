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

local OUTPUT_CHUNK_SIZE = 4096
local PROGRESS_UPDATE_INTERVAL = 4096
local PROGRESS_YIELD_INTERVAL = 16384

export type SerializedAnimation = {
	t: number,
	kfs: { { t: number, kf: { [string]: { components: { number }, easingStyle: string, easingDirection: string } }, fc: { [string]: { value: number, easingStyle: string, easingDirection: string } }? } },
	is_deform_rig: boolean,
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

type DeserializeProgressCallback = (progress: number, status: string?, detail: string?, canEstimate: boolean?) -> ()

local function formatDecodedSize(byteCount: number): string
	local kilobytes = byteCount / 1024
	if kilobytes >= 1024 then
		return string.format("decoded %.2f mb", kilobytes / 1024)
	end
	return string.format("decoded %.1f kb", kilobytes)
end

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
	local startTime = 0

	-- Sort in-place for better performance, then set start time to earliest keyframe
	table.sort(allKeyframes, function(a, b)
		return (a :: any).Time < (b :: any).Time
	end)
	startTime = (allKeyframes[1] :: any).Time

	-- Pre-cache common values to avoid repeated property access
	local isDeformRig = rig.isDeformRig
	local collectedCount = 0

	for i = 1, #allKeyframes do
		local kf = allKeyframes[i]
		if kf:IsA("Keyframe") then
			-- Pre-allocate state table as a proper dictionary
			local state = {}
			local faceState = {}
			
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
				elseif pose:IsA("NumberPose") then
					faceState[pose.Name] = {
						value = pose.Value,
						easingStyle = "Linear",
						easingDirection = "Out",
					}
				end
			end
			
			-- Only add keyframe if it has transform poses or face controls
			if next(state) or next(faceState) then
				collectedCount += 1
				local serializedKeyframe = {
					t = (kf :: any).Time - startTime, 
					kf = state 
				}
				if next(faceState) then
					(serializedKeyframe :: any).fc = faceState
				end
				collected[collectedCount] = serializedKeyframe
			end
		end
	end

	if collectedCount == 0 then
		warn("Animation has no poses or face controls to serialize.")
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
		is_deform_rig = isDeformRig,
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

function AnimationSerializer:deserialize(
	data: string,
	isBinary: boolean,
	progressCallback: DeserializeProgressCallback?
): any?
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

	local estimatedOutputSize = if isBinary then #data * 4 else math.floor(#data * 3)
	local estimatedChunkCount = math.max(1, math.ceil(estimatedOutputSize / OUTPUT_CHUNK_SIZE))
	local buffer = table.create(estimatedChunkCount)
	local bufferIndex = 0
	local chunk = table.create(OUTPUT_CHUNK_SIZE)
	local chunkIndex = 0

	local function flushChunk()
		if chunkIndex == 0 then
			return
		end

		bufferIndex += 1
		buffer[bufferIndex] = table.concat(chunk, "", 1, chunkIndex)
		table.clear(chunk)
		chunkIndex = 0
	end

	if progressCallback then
		progressCallback(
			0,
			"Decoding animation data",
			if isBinary then "reading compressed animation" else "reading encoded text animation",
			true
		)
	end

	-- Optimized byte collection function with yielding for large data
	local byteCount = 0
	local lastProgressByteCount = 0
	local lastYieldByteCount = 0
	local function collectByte(byte: number)
		chunkIndex += 1
		chunk[chunkIndex] = string.char(byte)
		byteCount += 1
		if chunkIndex >= OUTPUT_CHUNK_SIZE then
			flushChunk()
		end
		if progressCallback and byteCount - lastProgressByteCount >= PROGRESS_UPDATE_INTERVAL then
			lastProgressByteCount = byteCount
			progressCallback(
				math.min(byteCount / math.max(estimatedOutputSize, 1), 0.92),
				"Decompressing animation",
				formatDecodedSize(byteCount),
				true
			)
		end
		if byteCount - lastYieldByteCount >= PROGRESS_YIELD_INTERVAL then
			lastYieldByteCount = byteCount
			if progressCallback then
				progressCallback(
					math.min(byteCount / math.max(estimatedOutputSize, 1), 0.92),
					"Decompressing animation",
					formatDecodedSize(byteCount),
					true
				)
			end
			task.wait()
		end
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

	flushChunk()
	if progressCallback then
		progressCallback(0.96, "Parsing animation JSON", "finalizing animation payload", false)
	end

	-- Use table.concat with explicit length for better performance
	-- Yield before concat if buffer is large
	if bufferIndex > 1000 then
		task.wait()
	end
	local jsonStr = table.concat(buffer, "", 1, bufferIndex)

	-- Clear buffer to help GC
	table.clear(buffer)
	table.clear(chunk)

    -- Parse the JSON
    local jsonSuccess, jsonResult = pcall(function()
        return httpService:JSONDecode(jsonStr)
    end)

    if not jsonSuccess then
        warn("JSON parsing failed: " .. tostring(jsonResult))
        return nil
    end

	if progressCallback then
		local keyframeCount = 0
		if type(jsonResult) == "table" and type((jsonResult :: any).kfs) == "table" then
			keyframeCount = #((jsonResult :: any).kfs)
		end
		progressCallback(
			1,
			"Decoded animation",
			if keyframeCount > 0 then string.format("%d keyframes decoded", keyframeCount) else "decoded animation payload",
			true
		)
	end

    return jsonResult
end

return AnimationSerializer
