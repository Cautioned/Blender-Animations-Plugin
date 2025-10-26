--!native
--!strict

local Utils = {}

function Utils.scaleAnimation(keyframeSequence: KeyframeSequence, scaleFactor: number): KeyframeSequence
	assert(type(scaleFactor) == "number" and scaleFactor > 0, "Scale factor must be a positive number")

	local scaledSequence = keyframeSequence:Clone()
	for _, pose in pairs(scaledSequence:GetDescendants()) do
		if pose:IsA("Pose") then
			pose.CFrame = pose.CFrame + pose.CFrame.Position * (scaleFactor - 1)
		end
	end
	return scaledSequence
end

function Utils.getAnimDuration(keyframeSequence: { any }?)
	if keyframeSequence then
		local totalTime = 0
		for _, keyframe in ipairs(keyframeSequence) do
			totalTime = math.max(totalTime, keyframe.Time)
		end
		return totalTime
	end
	return 0
end

function Utils.getRealKeyframeDuration(keyframes: { Instance })
	local totalTime = 0
	for _, keyframe in ipairs(keyframes) do
		if keyframe:IsA("Keyframe") then
			totalTime = math.max(totalTime, (keyframe :: any).Time)
		end
	end
	return totalTime
end

return Utils 