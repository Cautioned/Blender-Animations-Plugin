--[[
	AnimationSimplifier.lua
	Reduces animation keyframe data for lower bandwidth/memory usage in-game.
	
	Strategies:
	1. Static bone removal -- bones whose CFrame never change across all keyframes are removed entirely.
	2. Keyframe thinning -- intermediate keyframes are removed if linear interpolation from neighbors
	   reproduces the value within a tolerance.
	3. Empty keyframe cleanup -- keyframes with no bone data (and optional no face controls) are removed,
	   except for the first and last frame to preserve duration.
	4. Precision rounding -- float values are rounded to a configurable decimal places.
]]

local M = {}

-- Default tolerances (near-lossless)
M.DEFAULT_POSITION_TOLERANCE = 0.002  -- studs -- 2 mm, barely perceptible
M.DEFAULT_ROTATION_TOLERANCE = 0.02   -- radians (~1.1 degrees)
M.DEFAULT_VALUE_TOLERANCE = 0.001     -- face controls / generic scalars
M.DEFAULT_DECIMAL_PLACES = 4

-- CFrame component layout from exporter: {px, py, pz, rxx, rxy, rxz, ryx, ryy, ryz, rzx, rzy, rzz}
local CFRAME_POSITION_START = 1
local CFRAME_POSITION_END = 3
local CFRAME_ROTATION_START = 4
local CFRAME_ROTATION_END = 12

local function deepCopy(t: any): any
	if type(t) ~= "table" then
		return t
	end
	local copy = {}
	for k, v in pairs(t) do
		copy[deepCopy(k)] = deepCopy(v)
	end
	return copy
end

local function lerp(a: number, b: number, t: number): number
	return a + (b - a) * t
end

local function roundValue(v: number, places: number): number
	local mult = 10 ^ places
	return math.round(v * mult) / mult
end

local function roundNumberArray(values: any, places: number)
	if type(values) ~= "table" then
		return
	end

	for i = 1, #values do
		if type(values[i]) == "number" then
			values[i] = roundValue(values[i], places)
		end
	end
end

local function roundCFrameData(cframeData: any, places: number)
	if type(cframeData) ~= "table" then
		return
	end

	if type(cframeData.components) == "table" then
		roundNumberArray(cframeData.components, places)
	elseif type(cframeData[1]) == "table" then
		roundNumberArray(cframeData[1], places)
	else
		roundNumberArray(cframeData, places)
	end
end

local function roundFaceControlData(controlData: any, places: number)
	if type(controlData) == "number" then
		return roundValue(controlData, places)
	end

	if type(controlData) ~= "table" then
		return controlData
	end

	if type(controlData.value) == "number" then
		controlData.value = roundValue(controlData.value, places)
	elseif type(controlData[1]) == "number" then
		controlData[1] = roundValue(controlData[1], places)
	end

	return controlData
end

local function arrayEqualWithinTolerance(a: {number}, b: {number}, startIdx: number, endIdx: number, tolerance: number): boolean
	for i = startIdx, endIdx do
		if math.abs((a[i] or 0) - (b[i] or 0)) > tolerance then
			return false
		end
	end
	return true
end

local function lerpArray(out: {number}, a: {number}, b: {number}, t: number, count: number)
	for i = 1, count do
		out[i] = lerp(a[i] or 0, b[i] or 0, t)
	end
end

--[[
	Remove bones that do not change across the entire animation.
	Returns the set of removed bone names and mutates keyframes in-place.
]]
function M.removeStaticBones(keyframes: {any}): {string}
	if not keyframes or #keyframes == 0 then
		return {}
	end

	local boneSet: {[string]: boolean} = {}
	for _, kf in ipairs(keyframes) do
		local pose = kf.kf or {}
		for boneName, _ in pairs(pose) do
			boneSet[boneName] = true
		end
	end

	local boneNames: {string} = {}
	for boneName, _ in pairs(boneSet) do
		table.insert(boneNames, boneName)
	end

	local firstPose = (keyframes[1].kf or {})
	local staticBones: {string} = {}
	for _, boneName in ipairs(boneNames) do
		local firstValue = firstPose[boneName]
		local isStatic = true
		for i = 2, #keyframes do
			local otherValue = (keyframes[i].kf or {})[boneName]
			if otherValue ~= firstValue then
				if type(firstValue) == "table" and type(otherValue) == "table" then
					if not arrayEqualWithinTolerance(firstValue, otherValue, 1, math.max(#firstValue, #otherValue), M.DEFAULT_VALUE_TOLERANCE) then
						isStatic = false
						break
					end
				else
					isStatic = false
					break
				end
			end
		end
		if isStatic then
			table.insert(staticBones, boneName)
		end
	end

	if #keyframes <= 2 then
		return {}
	end

	if #staticBones > 0 then
		for i = 2, #keyframes - 1 do
			local kf = keyframes[i]
			local pose = kf.kf
			if pose then
				for _, boneName in ipairs(staticBones) do
					pose[boneName] = nil
				end
			end
		end
	end

	return staticBones
end

--[[
	Thin keyframes for a single bone using tolerance-based interpolation test.
	Assumes keyframes are ordered by time.
	Returns a new array of kept keyframe indices.
]]
function M.thinBoneKeyframes(
	keyframes: {any},
	boneName: string,
	posTolerance: number,
	rotTolerance: number
): {number}
	-- Collect all keyframe indices where this bone exists
	local indices: {number} = {}
	local values: {{number}} = {}
	for i, kf in ipairs(keyframes) do
		local pose = kf.kf or {}
		local v = pose[boneName]
		if v and type(v) == "table" and #v >= 12 then
			table.insert(indices, i)
			table.insert(values, v)
		end
	end

	if #indices <= 2 then
		return indices
	end

	local kept: {number} = {indices[1]}
	local buf: {number} = table.create(12, 0)

	local anchorIdx = 1
	for testIdx = 2, #indices - 1 do
		local nextIdx = testIdx + 1
		local iA, iB = indices[anchorIdx], indices[nextIdx]
		local vA, vB = values[anchorIdx], values[nextIdx]

		-- Time proportional interpolation factor
		local tA = keyframes[iA].t or 0
		local tB = keyframes[iB].t or 0
		local tTest = keyframes[indices[testIdx]].t or 0
		local denom = tB - tA
		local t = if denom > 1e-8 then (tTest - tA) / denom else 0

		lerpArray(buf, vA, vB, t, 12)

		local vTest = values[testIdx]
		local posOk = arrayEqualWithinTolerance(buf, vTest, CFRAME_POSITION_START, CFRAME_POSITION_END, posTolerance)
		local rotOk = arrayEqualWithinTolerance(buf, vTest, CFRAME_ROTATION_START, CFRAME_ROTATION_END, rotTolerance)

		if posOk and rotOk then
			-- Keyframe can be dropped; keep anchor, continue testing
			continue
		else
			-- Cannot interpolate; keep the test keyframe as new anchor
			table.insert(kept, indices[testIdx])
			anchorIdx = testIdx
		end
	end

	-- Always keep last
	table.insert(kept, indices[#indices])
	return kept
end

--[[
	Run thinning across all bones and merge kept indices.
	A keyframe is kept if ANY bone needs it.
]]
function M.thinKeyframes(
	keyframes: {any},
	posTolerance: number?,
	rotTolerance: number?
): {any}
	posTolerance = posTolerance or M.DEFAULT_POSITION_TOLERANCE
	rotTolerance = rotTolerance or M.DEFAULT_ROTATION_TOLERANCE

	if not keyframes or #keyframes <= 2 then
		return keyframes
	end

	-- Gather all bone names
	local boneSet: {[string]: boolean} = {}
	for _, kf in ipairs(keyframes) do
		local pose = kf.kf or {}
		for boneName, _ in pairs(pose) do
			boneSet[boneName] = true
		end
	end

	-- Collect which keyframe indices to keep (union across all bones)
	local keepSet: {[number]: boolean} = {}
	keepSet[1] = true
	keepSet[#keyframes] = true

	for boneName, _ in pairs(boneSet) do
		local kept = M.thinBoneKeyframes(keyframes, boneName, posTolerance, rotTolerance)
		for _, idx in ipairs(kept) do
			keepSet[idx] = true
		end
	end

	-- Also keep any keyframe with face controls that differ from neighbors
	for i, kf in ipairs(keyframes) do
		if kf.fc then
			-- conservative: keep all face control keyframes
			keepSet[i] = true
		end
	end

	-- Build new keyframe array
	local result: {any} = {}
	for i, kf in ipairs(keyframes) do
		if keepSet[i] then
			table.insert(result, kf)
		end
	end

	return result
end

--[[
	Round all numeric values in CFrames and face controls to specified decimal places.
	Mutates in-place.
]]
function M.roundPrecision(keyframes: {any}, decimalPlaces: number?)
	decimalPlaces = decimalPlaces or M.DEFAULT_DECIMAL_PLACES

	for i, kf in ipairs(keyframes) do
		-- Skip first and last keyframe to preserve exact boundary poses
		if i == 1 or i == #keyframes then
			continue
		end
		local pose = kf.kf
		if pose then
			for _, cframe in pairs(pose) do
				roundCFrameData(cframe, decimalPlaces)
			end
		end

		local fc = kf.fc
		if fc then
			for controlName, controlData in pairs(fc) do
				fc[controlName] = roundFaceControlData(controlData, decimalPlaces)
			end
		end
	end
end

--[[
	Remove keyframes that have zero bone data and no face controls.
	Always preserves first and last keyframe.
]]
function M.removeEmptyKeyframes(keyframes: {any}): {any}
	if not keyframes or #keyframes <= 2 then
		return keyframes
	end

	local result: {any} = {keyframes[1]}
	for i = 2, #keyframes - 1 do
		local kf = keyframes[i]
		local hasBones = false
		if kf.kf then
			for _, _ in pairs(kf.kf) do
				hasBones = true
				break
			end
		end
		local hasFace = kf.fc and next(kf.fc) ~= nil
		if hasBones or hasFace then
			table.insert(result, kf)
		end
	end
	table.insert(result, keyframes[#keyframes])
	return result
end

--[[
	Compute the squared deviation of a keyframe from linear interpolation
	between its neighbors. Higher = more significant = should be kept.
	Aggregates deviation across all bones and face controls.
]]
local function computeKeyframeSignificance(keyframes: {any}, index: number): number
	if index <= 1 or index >= #keyframes then
		return math.huge -- always keep first and last
	end

	local prev = keyframes[index - 1]
	local curr = keyframes[index]
	local next = keyframes[index + 1]
	local maxDev = 0

	local tPrev = prev.t or 0
	local tNext = next.t or 0
	local tCurr = curr.t or 0
	local denom = tNext - tPrev
	local t = if denom > 1e-8 then (tCurr - tPrev) / denom else 0.5

	-- Position/rotation deviation across all bones
	for boneName, currVal in pairs(curr.kf or {}) do
		local prevVal = (prev.kf or {})[boneName]
		local nextVal = (next.kf or {})[boneName]
		if type(currVal) == "table" and #currVal >= 12
			and type(prevVal) == "table" and #prevVal >= 12
			and type(nextVal) == "table" and #nextVal >= 12 then
			-- Lerp each component and find max squared deviation
			for i = 1, 12 do
				local interpolated = prevVal[i] + (nextVal[i] - prevVal[i]) * t
				local dev = math.abs(currVal[i] - interpolated)
				if dev > maxDev then
					maxDev = dev
				end
			end
		end
	end

	-- Face control deviation
	for fcName, currVal in pairs(curr.fc or {}) do
		local prevVal = (prev.fc or {})[fcName]
		local nextVal = (next.fc or {})[fcName]
		if type(currVal) == "number" and type(prevVal) == "number" and type(nextVal) == "number" then
			local interpolated = prevVal + (nextVal - prevVal) * t
			local dev = math.abs(currVal - interpolated)
			if dev > maxDev then
				maxDev = dev
			end
		end
	end

	return maxDev
end

-- Angle (in radians) between two rotations given as CFrame component arrays.
-- Uses trace(R_a^T * R_b) = 1 + 2*cos(angle).
local function rotationAngle(a: {number}, b: {number}): number
	local ax, ay, az = a[4], a[5], a[6]
	local bx, by, bz = a[7], a[8], a[9]
	local cx, cy, cz = a[10], a[11], a[12]

	local dx, dy, dz = b[4], b[5], b[6]
	local ex, ey, ez = b[7], b[8], b[9]
	local fx, fy, fz = b[10], b[11], b[12]

	local rxx = ax*dx + bx*ex + cx*fx
	local ryy = ay*dy + by*ey + cy*fy
	local rzz = az*dz + bz*ez + cz*fz

	local trace = rxx + ryy + rzz
	trace = math.clamp(trace, -1, 3)
	return math.acos((trace - 1) / 2)
end

--[[
	Score rotation significance by how much angular velocity changes at this frame.
	Smooth constant-velocity rotation scores ~0; sharp rotational turns score high.
	Much more accurate than component-wise lerp (which is invalid for rotations).
]]
local function rotationSignificance(currVal: {number}, prevVal: {number}, nextVal: {number}, tCurr: number, tPrev: number, tNext: number): number
	local anglePrev = rotationAngle(currVal, prevVal)
	local angleNext = rotationAngle(currVal, nextVal)

	local dtPrev = math.max(tCurr - tPrev, 1e-8)
	local dtNext = math.max(tNext - tCurr, 1e-8)
	local velPrev = anglePrev / dtPrev
	local velNext = angleNext / dtNext

	-- Deviation from constant angular velocity (radians per second difference)
	return math.abs(velPrev - velNext)
end

--[[
	Compute a significance score for a keyframe using its active neighbors.
	Uses sum of squared deviations (not max) so frames with broad movement
	across many bones score higher. Position and rotation are weighted
	differently since they have different scales.
]]
local function computeKeyframeSignificanceWithNeighbors(keyframes: {any}, index: number, prevIdx: number, nextIdx: number): number
	if index <= 1 or index >= #keyframes then
		return math.huge
	end

	local prev = keyframes[prevIdx]
	local curr = keyframes[index]
	local next = keyframes[nextIdx]

	local tPrev = prev.t or 0
	local tNext = next.t or 0
	local tCurr = curr.t or 0
	local denom = tNext - tPrev
	local t = if denom > 1e-8 then (tCurr - tPrev) / denom else 0.5

	local posDevSq = 0
	local rotDevSq = 0
	local fcDevSq = 0

	-- Position/rotation deviation across all bones
	for boneName, currVal in pairs(curr.kf or {}) do
		local prevVal = (prev.kf or {})[boneName]
		local nextVal = (next.kf or {})[boneName]
		if type(currVal) == "table" and #currVal >= 12
			and type(prevVal) == "table" and #prevVal >= 12
			and type(nextVal) == "table" and #nextVal >= 12 then
			for i = 1, 3 do
				local interpolated = prevVal[i] + (nextVal[i] - prevVal[i]) * t
				local dev = currVal[i] - interpolated
				posDevSq += dev * dev
			end
			-- Angular velocity deviation (radians/sec) — valid for rotations unlike component lerp
			rotDevSq += rotationSignificance(currVal, prevVal, nextVal, tCurr, tPrev, tNext) ^ 2
		end
	end

	-- Face control deviation
	for fcName, currVal in pairs(curr.fc or {}) do
		local prevVal = (prev.fc or {})[fcName]
		local nextVal = (next.fc or {})[fcName]
		if type(currVal) == "number" and type(prevVal) == "number" and type(nextVal) == "number" then
			local interpolated = prevVal + (nextVal - prevVal) * t
			local dev = currVal - interpolated
			fcDevSq += dev * dev
		end
	end

	--[[
		Combine: position is dominant, rotation weighted ~0.3, face controls ~1.0.
		Using sqrt so scores are in original units (studs/radians) not squared.
	]]
	return math.sqrt(posDevSq) + math.sqrt(rotDevSq) * 0.3 + math.sqrt(fcDevSq)
end

--[[
	Rank keyframes by geometric significance and keep the top N.
	This gives predictable frame-count control (like Visvalingam-Whyatt).
	Much more robust than tolerance-based thinning for varying rig scales.
]]
function M.rankAndThinKeyframes(keyframes: {any}, targetCount: number): {any}
	if not keyframes or #keyframes <= 2 or targetCount >= #keyframes then
		return keyframes
	end
	targetCount = math.max(targetCount, 2)

	local total = #keyframes
	local toRemove = total - targetCount
	if toRemove <= 0 then
		return keyframes
	end

	-- active[i] = true if frame i is still in the sequence
	local active: {[number]: boolean} = {}
	for i = 1, total do
		active[i] = true
	end

	-- Helper: find previous active frame before index
	local function findPrev(idx: number): number?
		for j = idx - 1, 1, -1 do
			if active[j] then return j end
		end
		return nil
	end

	-- Helper: find next active frame after index
	local function findNext(idx: number): number?
		for j = idx + 1, total do
			if active[j] then return j end
		end
		return nil
	end

	-- Compute initial scores using immediate neighbors
	local scores: {[number]: number} = {}
	for i = 2, total - 1 do
		scores[i] = computeKeyframeSignificanceWithNeighbors(keyframes, i, i - 1, i + 1)
	end
	scores[1] = math.huge
	scores[total] = math.huge

	-- Iteratively remove least significant interior frames
	for _ = 1, toRemove do
		local minScore = math.huge
		local minIdx: number? = nil

		for i = 2, total - 1 do
			if active[i] and scores[i] < minScore then
				minScore = scores[i]
				minIdx = i
			end
		end

		if not minIdx then
			break
		end

		active[minIdx] = false

		-- Find neighbors of the removed frame
		local prevIdx = findPrev(minIdx)
		local nextIdx = findNext(minIdx)

		-- Recompute scores for neighbors since their adjacent frames changed
		if prevIdx and prevIdx > 1 then
			local prevPrev = findPrev(prevIdx)
			if prevPrev then
				scores[prevIdx] = computeKeyframeSignificanceWithNeighbors(keyframes, prevIdx, prevPrev, nextIdx or total)
			end
		end
		if nextIdx and nextIdx < total then
			local nextNext = findNext(nextIdx)
			if nextNext then
				scores[nextIdx] = computeKeyframeSignificanceWithNeighbors(keyframes, nextIdx, prevIdx or 1, nextNext)
			end
		end
	end

	-- Post-process: fill large gaps for mild simplification only.
	-- Aggressive drops (>50%) skip gap-filling so the targetCount is actually reached.
	local maxGap = if targetCount < total * 0.5
		then math.huge -- disable for aggressive simplification
		else math.max(5, math.floor(total / targetCount * 2))
	local changed = true
	while changed do
		changed = false
		local lastKept = 1
		for i = 2, total do
			if active[i] then
				local gap = i - lastKept - 1
				if gap > maxGap then
					-- Find the highest-scored removed frame in the gap
					local bestIdx: number? = nil
					local bestScore = -1
					for j = lastKept + 1, i - 1 do
						if not active[j] and scores[j] > bestScore then
							bestScore = scores[j]
							bestIdx = j
						end
					end
					if bestIdx then
						active[bestIdx] = true
						changed = true
					end
				end
				lastKept = i
			end
		end
	end

	-- Build result preserving original order
	local result: {any} = {}
	for i, kf in ipairs(keyframes) do
		if active[i] then
			table.insert(result, kf)
		end
	end

	return result
end

--[[
	Main entry point: simplify an animation payload.
	Supports both tolerance-based (legacy) and target-count (new) modes.
	Returns a new table with reduced keyframes and a summary string.
]]
function M.simplify(animationData: {any}, options: {any}?): ({any}, string)
	options = options or {}
	local targetCount = options.targetCount
	local decimalPlaces = options.decimalPlaces or M.DEFAULT_DECIMAL_PLACES
	local skipStaticBones = options.skipStaticBones == true
	local skipThinning = options.skipThinning == true
	local skipRounding = options.skipRounding == true
	local skipEmptyCleanup = options.skipEmptyCleanup == true

	local originalKfs = animationData.kfs
	if not originalKfs or #originalKfs == 0 then
		return animationData, "No keyframes to simplify."
	end

	local keyframes: {any} = deepCopy(originalKfs)

	local originalCount = #keyframes
	local summaryParts: {string} = {}

	-- 1. Static bone removal
	if not skipStaticBones then
		local removed = M.removeStaticBones(keyframes)
		if #removed > 0 then
			table.insert(summaryParts, string.format("removed %d static bones (%s)", #removed, table.concat(removed, ", ")))
		end
	end

	-- 2. Keyframe thinning
	if not skipThinning then
		if targetCount and targetCount >= 2 and targetCount < #keyframes then
			keyframes = M.rankAndThinKeyframes(keyframes, targetCount)
			local thinnedCount = originalCount - #keyframes
			if thinnedCount > 0 then
				table.insert(summaryParts, string.format("thinned %d frames (ranked, kept %d)", thinnedCount, #keyframes))
			end
		else
			-- Fallback to tolerance-based thinning
			local posTolerance = options.posTolerance or M.DEFAULT_POSITION_TOLERANCE
			local rotTolerance = options.rotTolerance or M.DEFAULT_ROTATION_TOLERANCE
			keyframes = M.thinKeyframes(keyframes, posTolerance, rotTolerance)
			local thinnedCount = originalCount - #keyframes
			if thinnedCount > 0 then
				table.insert(summaryParts, string.format("thinned %d frames (tol %.4f / %.4f)", thinnedCount, posTolerance, rotTolerance))
			end
		end
	end

	-- 3. Empty keyframe cleanup
	if not skipEmptyCleanup then
		keyframes = M.removeEmptyKeyframes(keyframes)
	end

	-- 4. Precision rounding
	if not skipRounding then
		M.roundPrecision(keyframes, decimalPlaces)
	end

	-- Build result
	local result = {}
	for k, v in pairs(animationData) do
		result[k] = v
	end
	result.kfs = keyframes

	local summary: string
	if #summaryParts > 0 then
		summary = string.format(
			"Simplified: %s. %d -> %d keyframes (%.1f%% reduction).",
			table.concat(summaryParts, "; "),
			originalCount,
			#keyframes,
			((originalCount - #keyframes) / originalCount) * 100
		)
	else
		summary = string.format("No simplification applied. %d keyframes remain.", originalCount)
	end

	return result, summary
end

return M
