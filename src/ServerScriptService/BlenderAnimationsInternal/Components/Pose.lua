--!native
--!strict
--!optimize 2

export type Pose = {
	rigPart: any,
	transform: CFrame,
	easingStyle: string,
	easingDirection: string,
}

local Pose = {}
Pose.__index = Pose

function Pose.new(rigPart: any, transform: CFrame, easingStyle: string?, easingDirection: string?): Pose
	local self: Pose = {
		rigPart = rigPart,
		transform = transform,
		easingStyle = easingStyle or "Linear",
		easingDirection = easingDirection or "In",
	}
	setmetatable(self, Pose)

	return self :: any
end

return Pose
