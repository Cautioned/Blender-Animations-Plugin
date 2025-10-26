--!native
--!strict

-- Type definitions for the new type solver
export type Connection = RBXScriptConnection

export type AnimationTrackType = {
	TimePosition: number,
	Length: number,
	IsPlaying: boolean?,
	Looped: boolean?,
	AdjustSpeed: (self: AnimationTrackType, speed: number) -> (),
	Stop: (self: AnimationTrackType, fadeTime: number?) -> (),
	Destroy: (self: AnimationTrackType) -> (),
	Play: (self: AnimationTrackType) -> (),
	Stopped: RBXScriptSignal,
	Name: string?,
}

export type AnimatorType = {
	FindFirstChildOfClass: (self: AnimatorType, className: string) -> Animator?,
	GetPlayingAnimationTracks: (self: AnimatorType) -> { AnimationTrack },
	LoadAnimation: (self: AnimatorType, animation: Animation) -> AnimationTrack,
	StepAnimations: (self: AnimatorType, delta: number) -> (),
}

export type RigModelType = Model & {
	PrimaryPart: BasePart?,
	FindFirstChildWhichIsA: (self: RigModelType, className: string) -> Instance?,
	FindFirstChild: (self: RigModelType, name: string) -> Instance?,
	GetScale: (self: RigModelType) -> number,
}

export type KeyframeType = {
	Time: number,
	time: number,
	transforms: { [string]: CFrame },
}

export type RigType = {
	model: RigModelType,
	root: any,
	bones: { [string]: any },
	keyframeNames: { any }?,
	EncodeRig: (self: RigType) -> any,
	LoadAnimation: (self: RigType, animData: any) -> (),
	ToRobloxAnimation: (self: RigType) -> KeyframeSequence,
	isDeformRig: boolean,
}

export type ArmatureInfo = {
	name: string,
	num_bones: number,
	has_animation: boolean?,
	frame_range: { number }?,
}

export type SavedAnimation = {
	name: string,
	instance: KeyframeSequence,
	duration: number,
}

export type KeyframeName = {
	name: string,
	time: number,
}

export type BoneWeight = {
	name: string,
	enabled: boolean,
	depth: number,
	parentName: string,
}

export type BoneWeightsList = { BoneWeight }

export type KeyframeStats = {
	count: number,
	totalDuration: number,
}

export type State = {
	liveSyncEnabled: any,
	lastKnownBlenderTimestamp: any,
}

return {} 