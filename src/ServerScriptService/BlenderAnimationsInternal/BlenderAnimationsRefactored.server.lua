





--!native
--!strict
--!optimize 2

local State = require(script.Parent.state)
local PlaybackService = require(script.Parent.Services.PlaybackService)

-- Import our new services
local RigManager = require(script.Parent.Services.RigManager)
local AnimationManager = require(script.Parent.Services.AnimationManager)
local BlenderSyncManager = require(script.Parent.Services.BlenderSyncManager)
local ExportManager = require(script.Parent.Services.ExportManager)
local CameraManager = require(script.Parent.Services.CameraManager)

-- Import UI components
local PlayerTab = require(script.Parent.UI.Tabs.PlayerTab)
local RiggingTab = require(script.Parent.UI.Tabs.RiggingTab)
local ToolsTab = require(script.Parent.UI.Tabs.ToolsTab)
local MoreTab = require(script.Parent.UI.Tabs.MoreTab)
local SharedComponents = require(script.Parent.UI.SharedComponents)
local RigTabs = require(script.Parent.UI.Components.RigTabs)
local RigSessionManager = require(script.Parent.RigSessionManager)

local Plugin = plugin

local Components = script.Parent.Components
local Packages = script.Parent.Packages

local PluginComponents = Components:FindFirstChild("PluginComponents")
local Widget = require(PluginComponents.Widget)
local Toolbar = require(PluginComponents.Toolbar)
local ToolbarButton = require(PluginComponents.ToolbarButton)
local Selection = game:GetService("Selection")
local UserInputService = game:GetService("UserInputService")
local StudioComponents = Components:FindFirstChild("StudioComponents")

local ScrollFrame = require(StudioComponents.ScrollFrame)
local Button = require(StudioComponents.Button)
local Label = require(StudioComponents.Label)
local Loading = require(StudioComponents.Loading)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local Fusion = require(Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed
local OnChange = Fusion.OnChange
local Observer = Fusion.Observer

local GLOBAL_HEADER_HEIGHT = 108
local RIG_TABS_HEIGHT = 28  -- 0 when no rigs, 28 when rigs present
-- Measured height of the auto-sizing global header (warnings can make it grow).
local globalHeaderHeight = Value(GLOBAL_HEADER_HEIGHT)

-- Initialize services
local playbackService = PlaybackService.new(State) :: any
local cameraManager = CameraManager.new()
local rigManager = RigManager.new(playbackService, cameraManager)
local animationManager = AnimationManager.new(playbackService, Plugin)
local exportManager = ExportManager.new(Plugin)
local blenderSyncManager = BlenderSyncManager.new(playbackService, animationManager)


State.rigManager = rigManager
local rigSessionManager = RigSessionManager.new(State)
State.rigSessionManager = rigSessionManager

-- Create services object for passing to UI components
local services = {
	playbackService = playbackService,
	rigManager = rigManager,
	animationManager = animationManager,
	exportManager = exportManager,
	cameraManager = cameraManager,
	blenderSyncManager = blenderSyncManager,
	plugin = Plugin,
}

local function importAnimationFromClipboard()
	if not State.enableClipboardExport:get() then
		warn("Clipboard import is disabled in Settings.")
		return
	end
	if not State.activeRigExists:get() then
		warn("No active rig selected.")
		return
	end

	services.playbackService:stopRigTrack(State.activeSessionRig:get())
	local importScriptText = "Paste the animation data below this line"

	services.exportManager:clearMetaParts()
	if State.importScript then
		State.importScript:Destroy()
	end
	State.importScript = Instance.new("Script", game.Workspace)
	assert(State.importScript)
	State.importScript.Archivable = false
	State.importScript.Source = "-- " .. importScriptText .. "\n"
	Plugin:OpenScript(State.importScript, 2)
	local tempConnection: RBXScriptConnection
	tempConnection = State.importScript.Changed:Connect(function(prop)
		if prop == "Source" then
			tempConnection:Disconnect()
			if State.importScript then
				local animData = select(
					3,
					string.find(
						State.importScript.Source,
						"^%-%- " .. importScriptText .. "\n(.*)$"
					)
				)
				State.importScript:Destroy()
				State.importScript = nil
				if animData then
					services.animationManager:loadAnimDataFromText(animData, false)
				end
			end
		end
	end)
end

local function cleanupAll()
	-- 1. Stop running processes (synchronously so it finishes before unload)
	playbackService:stopAnimationAndDisconnect()
	
	blenderSyncManager:cleanup()

	-- 2. Disconnect UI-related connections
	cameraManager:cleanup()

	-- 3. Reset state variables
	State.loadingEnabled:set(false)
	State.loadingTitle:set("Working")
	State.loadingStatus:set("Please wait...")
	State.loadingDetail:set("")
	State.loadingProgress:set(0)
	State.loadingCanEstimate:set(false)
	State.rigModelName:set("No Rig Selected")
	State.keyframeStats:set({ count = 0, totalDuration = 0 })
	State.playhead:set(0)
	State.keyframeNames:set({})
	State.savedAnimations:set({})
	State.selectedSavedAnim:set(nil)
	State.activeRigModel = nil
	State.activeAnimator = nil
	State.activeRig = nil
	State.currentKeyframeSequence = nil
	State.isPlaying:set(false)
	State.isReversed:set(false)
	State.animationData = nil
	State.isSelectionLocked:set(false)
	State.activeRigExists:set(false)
	State.isFinished:set(false)
	rigManager:clearWarnings()

	-- 4. Clear multi-rig sessions and playback tracks
	playbackService._rigTracks = {}
	rigSessionManager:clear()
	State.rigTabOrder:set({})
	State.isAwaitingRigSelection:set(false)
end


-- Function to update the active rig based on the current selection in Studio
local function updateActiveRigFromSelection()
	if State.widgetsEnabled:get(true) and not State.isSelectionLocked:get() then
		local selection = Selection:Get()
		if #selection > 0 then
			local selectedObject = selection[1]

			if rigManager:isKeyframeSequence(selectedObject) then
				State.lastSelectionWasKeyframeSequence = true
				return
			end

			if rigManager:isValidRig(selectedObject) then
				if State.lastSelectionWasKeyframeSequence then
					State.lastSelectionWasKeyframeSequence = false
					return
				end

				if rigSessionManager:contains(selectedObject) then
					RigTabs._switchToRig(services, selectedObject)
					return
				end

				local currentRig = State.activeSessionRig:get()
				if currentRig and not State.isAwaitingRigSelection:get() then
					-- Outside explicit multi-rig selection, replace the sole session.
					playbackService:stopRigTrack(currentRig)
					rigSessionManager:remove(currentRig)
					State.rigTabOrder:set({})
					State.activeSessionRig:set(nil)
				end

				rigSessionManager:createAndActivate(selectedObject)
				RigTabs.addRigTab(selectedObject)
				State.isAwaitingRigSelection:set(false)
				State.animationLength:set(0)
				State.animationData = nil
				State.animationDirty:set(false)
				State.activeRigExists:set(true)
				rigManager:clearWarnings()
				State.loadingEnabled:set(true)
				State.activeRigModel = selectedObject
				State.activeRig = nil
				rigSessionManager:saveActive()

				task.spawn(function()
					rigManager:setRig(selectedObject)
					if State.activeSessionRig:get() == selectedObject then
						rigSessionManager:saveActive()
					end
				end)
			end
		elseif #selection == 0 then
			State.lastSelectionWasKeyframeSequence = false
		end
	end
end

-- Start live sync if enabled when an armature is selected
table.insert(
	State.observers,
	Observer(State.selectedArmature):onChange(function()
		if State.selectedArmature:get() and State.liveSyncEnabled:get() then
			blenderSyncManager:startLiveSyncing()
		end
	end)
)

-- Prevent autoconnect from continuously retrying on state changes
local autoConnectAttemptedThisSession = false
table.insert(
	State.observers,
	Observer(State.autoConnectToBlender):onChange(function()
		if State.autoConnectToBlender:get() and not autoConnectAttemptedThisSession then
			-- Only allow autoconnect to attempt once per session to prevent continuous retries
			autoConnectAttemptedThisSession = true
			blenderSyncManager.autoConnectAttempts = 0 -- Reset for manual attempt
			task.spawn(function()
				task.wait(0.5)
				if blenderSyncManager.autoConnectAttempts < blenderSyncManager.maxAutoConnectAttempts then
					blenderSyncManager.autoConnectAttempts = (blenderSyncManager.autoConnectAttempts or 0) + 1
					blenderSyncManager:toggleServerConnection()
				end
			end)
		end
	end)
)

do -- Creates the plugin
	local pluginToolbar = Toolbar({
		Plugin = Plugin,
		Name = "Blender Animations",
	})

	local importClipboardAction = Plugin:CreatePluginAction(
		"BlenderAnimations_ImportClipboard",
		"Import Animation (Clipboard)",
		"Import animation data from clipboard",
		""
	)

	table.insert(
		State.connections,
		importClipboardAction.Triggered:Connect(function()
			State.activeTab:set("Player")
			importAnimationFromClipboard()
		end)
	)

	State.widgetsEnabled = Value(false)
	State.helpWidgetEnabled = Value(false)
	
	-- Update image based on current theme using themeProvider like PlaybackControls
	local function updateToolbarImage()
		local testColor = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground)
		-- Dark theme has darker background colors
		if testColor and testColor.R and testColor.G and testColor.B and testColor.R < 0.2 and testColor.G < 0.2 and testColor.B < 0.2 then
			State.toolbarButtonImage:set("rbxassetid://116041192227009") -- dark theme
		else
			State.toolbarButtonImage:set("rbxassetid://92189642379919") -- light theme
		end
	end
	
	-- Initial update
	updateToolbarImage()
	
	local enableButton = ToolbarButton({
		Plugin = Plugin,
		Toolbar = pluginToolbar,
		ClickableWhenViewportHidden = true,
		Name = "Open",
		ToolTip = "Open Blender Animations Plugin",
		Image = State.toolbarButtonImage:get(),

		[OnEvent("Click")] = function()
			(State.widgetsEnabled :: any):set(not (State.widgetsEnabled :: any):get())
		end,
	})
	
	
	-- Add observer for toolbar button image changes
	table.insert(State.observers, Observer(State.toolbarButtonImage):onChange(function()
		if enableButton and enableButton.Parent then
			pcall(function()
				enableButton.Image = State.toolbarButtonImage:get()
			end)
		end
	end))

	-- Handle plugin unloading
	Plugin.Unloading:Connect(function()
		-- Disconnect observers first to prevent them from firing during cleanup
		for _, obs in ipairs(State.observers) do
			obs()
		end
		table.clear(State.observers)

		-- Run cleanup synchronously to ensure stopAnimationAndDisconnect finishes before unload
		cleanupAll()

		if State.selectionConnection then
			State.selectionConnection:Disconnect()
			State.selectionConnection = nil
		end

		for _, conn in ipairs(State.connections) do
			conn:Disconnect()
		end
		table.clear(State.connections)
	end)

	-- Handle widget enabled/disabled
	table.insert(
		State.observers,
		(Observer(State.widgetsEnabled :: any) :: any):onChange(function(isEnabled: boolean)
				if enableButton and enableButton.Parent then
					enableButton:SetActive(isEnabled)
				end
				if isEnabled then
					updateActiveRigFromSelection()
				end
				return nil
			end) :: any
	)
	


	-- Load saved settings
    local savedDockSide = plugin:GetSetting("DockSide")
	if savedDockSide and typeof(savedDockSide) == "EnumItem" then
		State.dockSide:set(savedDockSide)
	end

    -- merge saved tab order with defaults (forward-compatible)
    do
        local defaults = { "Player", "Rigging", "Tools", "More" }
        local defaultSet = {}
        for _, name in ipairs(defaults) do defaultSet[name] = true end

        local saved = plugin:GetSetting("TabOrder")
        local merged = {}
        local seen = {}

        if typeof(saved) == "table" then
            for _, name in ipairs(saved) do
                if defaultSet[name] and not seen[name] then
                    table.insert(merged, name)
                    seen[name] = true
                end
            end
        end

        for _, name in ipairs(defaults) do
            if not seen[name] then
                table.insert(merged, name)
            end
        end

        State.tabs:set(merged)
    end

    -- load persisted settings (tools/settings toggles)
    local ef = plugin:GetSetting("EnableFileExport")
    if typeof(ef) == "boolean" then
        State.enableFileExport:set(ef)
    else
        State.enableFileExport:set(true)
    end
    local ec = plugin:GetSetting("EnableClipboardExport")
    if typeof(ec) == "boolean" then
        State.enableClipboardExport:set(ec)
    else
        State.enableClipboardExport:set(true)
    end
    local els = plugin:GetSetting("EnableLiveSync")
    if typeof(els) == "boolean" then State.enableLiveSync:set(els) end
    local ac = plugin:GetSetting("AutoConnectToBlender")
    if typeof(ac) == "boolean" then State.autoConnectToBlender:set(ac) end
	local rm = plugin:GetSetting("ReducedMotion")
	if typeof(rm) == "boolean" then State.reducedMotion:set(rm) end
    local sd = plugin:GetSetting("ShowDebugInfo")
    if typeof(sd) == "boolean" then State.showDebugInfo:set(sd) end
    
    -- Auto-connect to Blender if enabled
    if State.autoConnectToBlender:get() then
        task.spawn(function()
            task.wait(1) -- Wait a bit for everything to initialize
            -- Add safeguard to prevent continuous retry attempts
            blenderSyncManager.autoConnectAttempts = (blenderSyncManager.autoConnectAttempts or 0) + 1
            if blenderSyncManager.autoConnectAttempts > blenderSyncManager.maxAutoConnectAttempts then
                warn("Auto-connect to Blender failed after " .. blenderSyncManager.maxAutoConnectAttempts .. " attempts. Giving up.")
                return
            end
            blenderSyncManager:toggleServerConnection()
        end)
    end

	-- Create tabs UI
	local function createTabsUI()
		return New("Frame")({
			Size = UDim2.new(0, 40, 1, 0),
			Position = Computed(function()
				return if State.dockSide:get() == Enum.InitialDockState.Left
					then UDim2.fromOffset(0, 0)
					else UDim2.new(1, -40, 0, 0)
			end),
			BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground),
			[Children] = {
				New("ScrollingFrame")({
					Size = UDim2.new(1, 0, 1, 0),
					BackgroundTransparency = 1,
					CanvasSize = UDim2.new(0, 0, 0, 0),
					AutomaticCanvasSize = Enum.AutomaticSize.Y,
					ScrollBarThickness = 4,
					ScrollBarImageTransparency = 0.5,
					[Children] = {
						New("UIListLayout")({
							FillDirection = Enum.FillDirection.Vertical,
							SortOrder = Enum.SortOrder.LayoutOrder,
							Padding = UDim.new(0, 4),
							HorizontalAlignment = Enum.HorizontalAlignment.Center,
						}),
						New("UIPadding")({
							PaddingTop = UDim.new(0, 10),
							PaddingBottom = UDim.new(0, 5),
						}),

						[Children :: any] = Computed(function()
							local tabButtons: {any} = {}

							local function DropIndicator(index: number)
								return New("Frame")({
									Size = UDim2.new(1, 0, 0, 4),
									BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
									BorderSizePixel = 0,
									LayoutOrder = index - 1,
									Visible = Computed(function()
										return State.dropIndex:get() == index
									end),
								})
							end

							for i, tabName in ipairs(State.tabs:get()) do
								table.insert(tabButtons, DropIndicator(i))
								table.insert(
									tabButtons,
									New("Frame")({
										LayoutOrder = i,
										Size = UDim2.new(1, 0, 0, 128),
										BackgroundTransparency = 1,
										[Children] = {
											Button({
												Text = tabName,
											Size = UDim2.new(0, 128, 0, 22),
												Position = UDim2.fromScale(0.5, 0.5),
												AnchorPoint = Vector2.new(0.5, 0.5),
												Rotation = Computed(function()
													return if State.dockSide:get() == Enum.InitialDockState.Left then 90 else -90
												end),
												BackgroundColorStyle = Computed(function()
													if State.activeTab:get() == tabName then
														return Enum.StudioStyleGuideColor.DiffFilePathBackground
													else
														return Enum.StudioStyleGuideColor.Button
													end
												end),
												[OnEvent("InputBegan")] = function(input)
													if input and input.UserInputType == Enum.UserInputType.MouseButton1 then
														State.activeTab:set(tabName)
														State.draggedTab:set(tabName)
													end
												end,
												[OnEvent("InputEnded")] = function(input)
													if input and input.UserInputType == Enum.UserInputType.MouseButton1 then
														local dropIndexValue = State.dropIndex:get()
														if State.draggedTab:get() and dropIndexValue then
															local tabs = State.tabs:get()
															local dragIndex
															for i, t in ipairs(tabs) do
																if t == State.draggedTab:get() then
																	dragIndex = i
																	break
																end
															end
															if dragIndex then
																local droppedTab = table.remove(tabs, dragIndex)
																local dropIndex = dropIndexValue
																if dragIndex < dropIndex then
																	dropIndex = dropIndex - 1
																end
																table.insert(tabs, dropIndex, droppedTab)
																State.tabs:set(tabs)
																plugin:SetSetting("TabOrder", tabs)
															end
														end
														State.draggedTab:set(nil)
														State.dropIndex:set(nil)
													end
												end,
											}) :: any,
											New("Frame")({
												Size = UDim2.fromScale(1, 1),
												BackgroundTransparency = 1,
												ZIndex = 2,
												[OnEvent("MouseEnter")] = function()
													if State.draggedTab:get() and State.draggedTab:get() ~= tabName then
														State.dropIndex:set(i)
													end
												end,
												[OnEvent("MouseLeave")] = function()
													if State.dropIndex:get() == i then
														State.dropIndex:set(nil)
													end
												end,
											}),
										},
									})
								)
							end
							table.insert(tabButtons, DropIndicator(#State.tabs:get() + 1))

							table.insert(
								tabButtons,
								New("Frame")({
									LayoutOrder = #State.tabs:get() + 2,
									Size = UDim2.new(1, 0, 0, 40),
									BackgroundTransparency = 1,
									[Children] = {
										Button({
											Text = "⇩",
											Size = UDim2.new(0, 40, 0, 22),
											Position = UDim2.fromScale(0.5, 0.5),
											AnchorPoint = Vector2.new(0.5, 0.5),
											Rotation = Computed(function()
												return if State.dockSide:get() == Enum.InitialDockState.Left then -90 else 90
											end),
											Activated = function()
												local newSide
												if State.dockSide:get() == Enum.InitialDockState.Left then
													newSide = Enum.InitialDockState.Right
												else
													newSide = Enum.InitialDockState.Left
												end
												State.dockSide:set(newSide)
												plugin:SetSetting("DockSide", newSide)
											end,
											BackgroundColorStyle = Enum.StudioStyleGuideColor.Button,
										}) :: any,
									},
								})
							)
							return tabButtons
						end, Fusion.cleanup) :: any,
					},
				}),
			},
		})
	end

	-- Build tab content once to avoid re-parent churn on tab switches
	local tabContent = {
		Player = PlayerTab.create(services),
		Rigging = RiggingTab.create(services),
		Tools = ToolsTab.create(services),
		More = MoreTab.create(services),
	}

	-- Build rig tabs bar once
	local rigTabsBar = RigTabs.create(services)

	-- Dynamic Y offset (rig tabs bar is always present with at least the + button)
	local rigTabsOffset = RIG_TABS_HEIGHT

	local function makeTabFrame(tabName: string, children)
		local tabChildren = {
			_UIListLayout = New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 7),
			}),
			_UIPadding = New("UIPadding")({
				PaddingLeft = UDim.new(0, 5),
				PaddingRight = UDim.new(0, 14),
				PaddingBottom = UDim.new(0, 10),
				PaddingTop = UDim.new(0, 10),
			}),
		}
		for i, child in ipairs(children) do
			tabChildren["child" .. i] = child
		end
		return New("Frame")({
			Name = tabName .. "Tab",
			BackgroundTransparency = 1,
			Size = Computed(function()
				return if State.activeTab:get() == tabName
					then UDim2.new(1, 0, 0, 0)
					else UDim2.new(1, 0, 0, 0)
			end),
			AutomaticSize = Computed(function()
				return if State.activeTab:get() == tabName
					then Enum.AutomaticSize.Y
					else Enum.AutomaticSize.None
			end),
			Visible = Computed(function()
				return State.activeTab:get() == tabName
			end),
			[Children] = tabChildren,
		})
	end

	local tabFrames = {
		_PlayerTab = makeTabFrame("Player", tabContent.Player),
		_RiggingTab = makeTabFrame("Rigging", tabContent.Rigging),
		_ToolsTab = makeTabFrame("Tools", tabContent.Tools),
		_MoreTab = makeTabFrame("More", tabContent.More),
	}

	local function handleMainWidgetEnabledChanged(isEnabled: boolean)
		if not isEnabled then
			cleanupAll()
		end
		if State.widgetsEnabled:get() ~= isEnabled then
			(State.widgetsEnabled :: any):set(isEnabled)
		end
		if isEnabled then
			updateActiveRigFromSelection()
		end
	end

	local function handleHelpWidgetEnabledChanged(isEnabled: boolean)
		if State.helpWidgetEnabled:get() ~= isEnabled then
			(State.helpWidgetEnabled :: any):set(isEnabled)
		end
	end

	-- Create the main widget
	local function pluginWidget()
		-- The header auto-sizes with its content (warnings can add lines), so
		-- the scroll area below offsets by the measured height instead of a
		-- fixed constant; otherwise long warnings overflow the header box.
		local globalHeaderFrame = nil :: any
		local globalHeader = New("Frame")({
			Name = "GlobalHeader",
			ZIndex = 1,
			Size = UDim2.new(1, -40, 0, 0),
			AutomaticSize = Enum.AutomaticSize.Y,
			Position = Computed(function()
				return if State.dockSide:get() == Enum.InitialDockState.Left
					then UDim2.fromOffset(40, rigTabsOffset)
					else UDim2.fromOffset(0, rigTabsOffset)
			end),
			BackgroundTransparency = 1,
			[Children] = {
				SharedComponents.createHeaderUI(services),
				New("Frame")({
					Name = "HeaderDivider",
					AnchorPoint = Vector2.new(0, 1),
					Position = UDim2.new(0, 0, 1, 0),
					Size = UDim2.new(1, 0, 0, 1),
					BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
					BorderSizePixel = 0,
				}),
			},
			[OnChange("AbsoluteSize")] = function()
				if globalHeaderFrame then
					globalHeaderHeight:set((globalHeaderFrame :: Frame).AbsoluteSize.Y)
				end
			end,
		})
		globalHeaderFrame = globalHeader

		return Widget({
			Plugin = Plugin,
			Id = "BlenderAnimationsMain",
			Name = "Blender Animations",
			InitialDockTo = State.dockSide:get(),
			InitialEnabled = false,
			ForceInitialEnabled = false,
			FloatingSize = Vector2.new(250, 600),
			MinimumSize = Vector2.new(250, 600),
			Enabled = State.widgetsEnabled,
			[Children] = New("Frame")({
				Size = UDim2.fromScale(1, 1),
				BackgroundTransparency = 1,
				[Children] = {
					createTabsUI(),
					-- Horizontal rig tabs (multi-rig support)
					New("Frame")({
						Name = "RigTabsContainer",
						ZIndex = 2,
						Size = UDim2.new(1, -40, 0, RIG_TABS_HEIGHT),
						Position = Computed(function()
							return if State.dockSide:get() == Enum.InitialDockState.Left
								then UDim2.fromOffset(40, 0)
								else UDim2.fromOffset(0, 0)
						end),
						BackgroundTransparency = 1,
						[Children] = {
							rigTabsBar,
						},
					}),
					globalHeader,
					ScrollFrame({
						ZIndex = 1,
						Size = Computed(function()
							return UDim2.new(1, -40, 1, -(globalHeaderHeight:get() + rigTabsOffset))
						end),
						Position = Computed(function()
							return if State.dockSide:get() == Enum.InitialDockState.Left
								then UDim2.fromOffset(40, globalHeaderHeight:get() + rigTabsOffset)
								else UDim2.fromOffset(0, globalHeaderHeight:get() + rigTabsOffset)
						end),
						BackgroundTransparency = 1,
						AutomaticCanvasSize = Enum.AutomaticSize.Y,
						[Children] = {
							_PlayerTab = tabFrames._PlayerTab,
							_RiggingTab = tabFrames._RiggingTab,
							_ToolsTab = tabFrames._ToolsTab,
							_MoreTab = tabFrames._MoreTab,
						},
					}),
					Loading({
						Enabled = State.loadingEnabled,
						Title = State.loadingTitle,
						Status = State.loadingStatus,
						Detail = State.loadingDetail,
						Progress = State.loadingProgress,
						CanEstimate = State.loadingCanEstimate,
					}),
			},
			}),
		})
	end

	-- Create the main widget with tab content
	local mainWidget = pluginWidget()
	table.insert(State.connections, mainWidget:GetPropertyChangedSignal("Enabled"):Connect(function()
		handleMainWidgetEnabledChanged(mainWidget.Enabled)
	end))
	mainWidget:BindToClose(function()
		handleMainWidgetEnabledChanged(false)
	end)
	

end

if not State.selectionConnection or not State.selectionConnection.Connected then
	State.selectionConnection = Selection.SelectionChanged:Connect(updateActiveRigFromSelection)
end

-- Tab hotkey (Tab / Shift+Tab) to cycle tabs
table.insert(
	State.connections,
	UserInputService.InputBegan:Connect(function(input, gameProcessed)
		if gameProcessed then
			return
		end
		if not State.widgetsEnabled:get() then
			return
		end
		if UserInputService:GetFocusedTextBox() then
			return
		end
		if input.KeyCode ~= Enum.KeyCode.Tab then
			return
		end

		local tabs = State.tabs:get()
		if #tabs == 0 then
			return
		end
		local current = State.activeTab:get()
		local currentIndex = 1
		for i, t in ipairs(tabs) do
			if t == current then
				currentIndex = i
				break
			end
		end
		local isShift = UserInputService:IsKeyDown(Enum.KeyCode.LeftShift)
			or UserInputService:IsKeyDown(Enum.KeyCode.RightShift)
		local step = isShift and -1 or 1
		local nextIndex = currentIndex + step
		if nextIndex > #tabs then
			nextIndex = 1
		elseif nextIndex < 1 then
			nextIndex = #tabs
		end
		State.activeTab:set(tabs[nextIndex])
	end)
)



