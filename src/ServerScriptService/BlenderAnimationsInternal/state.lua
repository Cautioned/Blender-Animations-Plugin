--!native
--!strict

local Fusion = require(script.Parent.Packages.Fusion)
local Types = require(script.Parent.types)

local Value = Fusion.Value
local Computed = Fusion.Computed

local State = {
    activeTab = Value("Player"),
	tabs = Value({ "Player", "Rigging", "Blender Sync", "Tools", "More" }),
	draggedTab = Value(nil),
	dropIndex = Value(nil),
	dockSide = Value(Enum.InitialDockState.Left),
	widgetsEnabled = Value(false),
	selectedPriority = Value("Action"),
	playhead = Value(0),
	rigModelName = Value("No Rig Selected"),
    activeWarnings = Value({} :: { string }),
    keyframeNames = Value({} :: { Types.KeyframeName }),
    keyframeNameInput = Value("Name"),
    animationPriorityOptions = { "Action", "Action2", "Action3", "Action4", "Core", "Idle", "Movement" },

    isFinished = Value(false),

    activeRigModel = nil :: Types.RigModelType?,
    activeAnimator = nil :: Types.AnimatorType?,
    activeRig = nil :: Types.RigType?,
    activeRigExists = Value(false),
    importScript = nil :: Script?,
    currentKeyframeSequence = nil :: KeyframeSequence?,

    isPlaying = Value(true),
    isReversed = Value(false),
    playPauseButtonImage = Value("rbxasset://textures/AnimationEditor/button_control_play.png"),
    reversePlayPauseButtonImage = Value("rbxasset://textures/AnimationEditor/button_control_reverseplay.png"),
    loopAnimation = Value(true),

    animationLength = Value(0),

    userChangingSlider = Value(false),
    uniqueNames = Value(true),

    animationData = nil :: { Types.KeyframeType }?,
    currentAnimTrack = nil :: Types.AnimationTrackType?,
    animationName = "KeyframeSequence",

    loadingEnabled = Value(false),
    keyframeStats = Value({ count = 0, totalDuration = 0 } :: Types.KeyframeStats),
    stopSpeed = Value(2),
    setRigOrigin = Value(true),

    lastSelectionWasKeyframeSequence = false,
    isSelectionLocked = Value(false),
    scaleFactor = Value(1),
    rigScale = Value(1),

    isServerConnected = Value(false),
    serverPort = Value(31337),

    availableArmatures = Value({}),
    selectedArmature = Value(nil),
    serverStatus = Value("Disconnected"),

    savedAnimations = Value({} :: { Types.SavedAnimation }),
    selectedSavedAnim = Value(nil),

    connections = {} :: { RBXScriptConnection },
    observers = {} :: { () -> () },

    cameraConnection = nil :: RBXScriptConnection?,
    selectionConnection = nil :: RBXScriptConnection?,

    heartbeat = { conn = nil :: RBXScriptConnection? },

    selectedPart = Value(nil),
    isCameraAttached = Value(false),
    fovValue = Value(game.Workspace.CurrentCamera.FieldOfView),
    
    metaParts = {} :: { Part | Model },

    liveSyncEnabled = Value(false),
    lastKnownBlenderAnimHash = Value(""),
    liveSyncCoroutine = nil :: thread?,
    
    -- Bone weights for toggling bones on/off
    boneWeights = Value({} :: Types.BoneWeightsList),

    -- Settings
    enableFileExport = Value(true),
    enableClipboardExport = Value(true),
    enableLiveSync = Value(false),
    autoConnectToBlender = Value(false),
    showDebugInfo = Value(true),
    
    -- Toolbar button image
    toolbarButtonImage = Value("rbxassetid://116041192227009"),
    
    -- Help widget
    helpWidgetEnabled = Value(false)
}

State.displayWarnings = Computed(function()
    local warnings = State.activeWarnings:get()
    if #warnings == 0 then
        return "No warnings. Awesome."
    else
        return table.concat(warnings, "\n \n")
    end
end)

return State 