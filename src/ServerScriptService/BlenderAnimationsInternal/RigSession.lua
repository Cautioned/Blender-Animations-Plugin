--!native
--!strict

-- Per-rig session state.  Saves/restores rig-specific State fields so
-- multiple rigs can coexist with independent playback, keyframes, etc.

local RigSession = {}

export type Snapshot = {
	-- Rig references
	activeRigModel: Instance?,
	activeAnimator: Instance?,
	activeRig: any?,
	rigModelName: string,
	activeRigExists: boolean,
	lastKnownRigModel: Instance?,
	rigScale: number,

	-- Animation data (loaded into the rig, used by playAllRigs)
	animationData: { any }?,
	currentKeyframeSequence: Instance?,

	-- Per-rig UI state
	keyframeNames: { any },
	keyframeStats: { count: number, totalDuration: number },
	savedAnimations: { any },
	selectedSavedAnim: any?,
	boneWeights: { any },
	exportBoneWeights: { any },
	activeWarnings: { string },
	selectedPriority: string,
	animationName: string,
	stopSpeed: number,
	setRigOrigin: boolean,
	scaleFactor: number,
	animationModifierStack: { any },
	mirrorAnimationEnabled: boolean,
	speedEnabled: boolean,
	speedMultiplier: number,
	resampleEnabled: boolean,
	resampleFps: number,
	simplifierEnabled: boolean,
	simplifierStrength: number,
	uniqueNames: boolean,
	isSelectionLocked: boolean,
	isAnimationDirty: boolean,
}

-- Capture everything rig-specific from global State into a snapshot table.
-- Playback fields (playhead, isPlaying, isReversed, loopAnimation, isFinished,
-- animationLength, currentAnimTrack) are intentionally excluded — they are global,
-- shared across all rigs.
function RigSession.createSnapshot(state: any): Snapshot
	return {
		activeRigModel = state.activeRigModel,
		activeAnimator = state.activeAnimator,
		activeRig = state.activeRig,
		rigModelName = state.rigModelName:get(),
		activeRigExists = state.activeRigExists:get(),
		lastKnownRigModel = state.lastKnownRigModel,
		rigScale = state.rigScale:get(),
		animationData = state.animationData,
		currentKeyframeSequence = state.currentKeyframeSequence,
		keyframeNames = state.keyframeNames:get(),
		keyframeStats = state.keyframeStats:get(),
		savedAnimations = state.savedAnimations:get(),
		selectedSavedAnim = state.selectedSavedAnim:get(),
		boneWeights = state.boneWeights:get(),
		exportBoneWeights = state.exportBoneWeights:get(),
		activeWarnings = state.activeWarnings:get(),
		selectedPriority = state.selectedPriority:get(),
		animationName = state.animationName,
		stopSpeed = state.stopSpeed:get(),
		setRigOrigin = state.setRigOrigin:get(),
		scaleFactor = state.scaleFactor:get(),
		animationModifierStack = state.animationModifierStack:get(),
		mirrorAnimationEnabled = state.mirrorAnimationEnabled:get(),
		speedEnabled = state.speedEnabled:get(),
		speedMultiplier = state.speedMultiplier:get(),
		resampleEnabled = state.resampleEnabled:get(),
		resampleFps = state.resampleFps:get(),
		simplifierEnabled = state.simplifierEnabled:get(),
		simplifierStrength = state.simplifierStrength:get(),
		uniqueNames = state.uniqueNames:get(),
		isSelectionLocked = state.isSelectionLocked:get(),
		isAnimationDirty = state.animationDirty:get(),
	}
end

-- Restore a previously saved snapshot into global State.
-- Playback fields are NOT restored — they remain at whatever the global
-- playback state currently is.
function RigSession.restoreSnapshot(state: any, snapshot: Snapshot)
	state.activeRigModel = snapshot.activeRigModel
	state.activeAnimator = snapshot.activeAnimator
	state.activeRig = snapshot.activeRig
	state.rigModelName:set(snapshot.rigModelName)
	state.activeRigExists:set(snapshot.activeRigExists)
	state.lastKnownRigModel = snapshot.lastKnownRigModel
	state.rigScale:set(snapshot.rigScale)
	state.animationData = snapshot.animationData
	state.currentKeyframeSequence = snapshot.currentKeyframeSequence
	state.keyframeNames:set(snapshot.keyframeNames)
	state.keyframeStats:set(snapshot.keyframeStats)
	state.savedAnimations:set(snapshot.savedAnimations)
	state.selectedSavedAnim:set(snapshot.selectedSavedAnim)
	state.boneWeights:set(snapshot.boneWeights)
	state.exportBoneWeights:set(snapshot.exportBoneWeights)
	state.activeWarnings:set(snapshot.activeWarnings)
	state.selectedPriority:set(snapshot.selectedPriority)
	state.animationName = snapshot.animationName
	state.stopSpeed:set(snapshot.stopSpeed)
	state.setRigOrigin:set(snapshot.setRigOrigin)
	state.scaleFactor:set(snapshot.scaleFactor)
	if snapshot.animationModifierStack then
		state.animationModifierStack:set(snapshot.animationModifierStack)
	end
	state.mirrorAnimationEnabled:set(snapshot.mirrorAnimationEnabled == true)
	state.speedEnabled:set(snapshot.speedEnabled == true)
	state.speedMultiplier:set(snapshot.speedMultiplier or 1)
	state.resampleEnabled:set(snapshot.resampleEnabled == true)
	state.resampleFps:set(snapshot.resampleFps or 24)
	state.simplifierEnabled:set(snapshot.simplifierEnabled == true)
	state.simplifierStrength:set(snapshot.simplifierStrength or 15)
	state.uniqueNames:set(snapshot.uniqueNames)
	state.isSelectionLocked:set(snapshot.isSelectionLocked)
	state.animationDirty:set(snapshot.isAnimationDirty == true)
end

return RigSession
