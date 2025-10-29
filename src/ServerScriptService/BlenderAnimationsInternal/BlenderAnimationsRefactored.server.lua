





--!native
--!strict
--!optimize 2

local State = require(script.Parent.state)
local Types = require(script.Parent.types)
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
local BlenderSyncTab = require(script.Parent.UI.Tabs.BlenderSyncTab)
local ToolsTab = require(script.Parent.UI.Tabs.ToolsTab)
local MoreTab = require(script.Parent.UI.Tabs.MoreTab)

local Plugin = plugin

local Components = script.Parent.Components
local Packages = script.Parent.Packages

local PluginComponents = Components:FindFirstChild("PluginComponents")
local Widget = require(PluginComponents.Widget)
local Toolbar = require(PluginComponents.Toolbar)
local ToolbarButton = require(PluginComponents.ToolbarButton)
local Selection = game:GetService("Selection")
local StudioComponents = Components:FindFirstChild("StudioComponents")

local ScrollFrame = require(StudioComponents.ScrollFrame)
local Button = require(StudioComponents.Button)
local Label = require(StudioComponents.Label)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local Fusion = require(Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed
local Observer = Fusion.Observer

-- Initialize services
local playbackService = PlaybackService.new(State, Types) :: any
local cameraManager = CameraManager.new()
local rigManager = RigManager.new(playbackService, cameraManager)
local animationManager = AnimationManager.new(playbackService, Plugin)
local exportManager = ExportManager.new()
local blenderSyncManager = BlenderSyncManager.new(playbackService, animationManager)

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

local function cleanupAll()
	-- 1. Stop running processes
	playbackService:stopAnimationAndDisconnect()
	blenderSyncManager:cleanup()

	-- 2. Disconnect UI-related connections
	cameraManager:cleanup()
	if State.selectionConnection then
		State.selectionConnection:Disconnect()
		State.selectionConnection = nil
	end

	-- 4. Reset state variables
	State.loadingEnabled:set(false)
	State.rigModelName:set("No Rig Selected")
	State.keyframeStats:set({ count = 0, totalDuration = 0 })
	State.playhead:set(0)
	State.keyframeNames:set({})
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
end

local function cleanupRigSelection()
	-- This function is a subset of cleanupAll, intended for when a rig is deselected.
	-- It resets rig-specific state without killing the Blender connection.
	playbackService:stopAnimationAndDisconnect()

	-- Reset state variables related to the rig
	State.loadingEnabled:set(false)
	State.rigModelName:set("No Rig Selected")
	State.keyframeStats:set({ count = 0, totalDuration = 0 })
	State.playhead:set(0)
	State.keyframeNames:set({})
	State.activeRigModel = nil
	State.activeAnimator = nil
	State.activeRig = nil
	State.currentKeyframeSequence = nil
	State.isPlaying:set(false)
	State.isReversed:set(false)
	State.animationData = nil
	State.activeRigExists:set(false)
	State.isFinished:set(false)
	rigManager:clearWarnings()
end

-- Function to update the active rig based on the current selection in Studio
local function updateActiveRigFromSelection()
	if State.widgetsEnabled:get(true) and not State.isSelectionLocked:get() then
		local selectedRig = false
		local selection = Selection:Get()
		if #selection > 0 and not selectedRig then
			local selectedObject = selection[1] -- Consider the first object in the selection

			if rigManager:isKeyframeSequence(selectedObject) then
				-- Set the flag if a KeyframeSequence is selected and do nothing
				State.lastSelectionWasKeyframeSequence = true
				return
			end

			if rigManager:isValidRig(selectedObject) then
				if State.lastSelectionWasKeyframeSequence then
					-- If the last selection was a KeyframeSequence, do not update the rig
					State.lastSelectionWasKeyframeSequence = false
					return
				end

				if State.activeRigModel ~= selectedObject then
					-- Proceed to set the rig only if it is valid, not a KeyframeSequence, and different from the current rig
					State.animationLength:set(0)
					State.animationData = nil
					State.activeRigExists:set(true)
					rigManager:clearWarnings()
					State.loadingEnabled:set(true) -- Enable loading indicator
					State.activeRig = selectedObject
					task.spawn(function()
						rigManager:setRig(selectedObject)
					end)
				end
				selectedRig = true
			else
				cleanupRigSelection()
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

do -- Creates the plugin
	local pluginToolbar = Toolbar({
		Name = "Blender Animations",
	})

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
		Toolbar = pluginToolbar,
		ClickableWhenViewportHidden = true,
		Name = "Open",
		ToolTip = "Open Blender Animations Plugin",
		Image = State.toolbarButtonImage:get(),

		[OnEvent("Click")] = function()
			(State.widgetsEnabled :: any):set(not (State.widgetsEnabled :: any):get())
		end,
	})
	
	local helpButton = ToolbarButton({
		Toolbar = pluginToolbar,
		ClickableWhenViewportHidden = true,
		Name = "READ!!!",
		ToolTip = "Open Help Widget",
		Image = "rbxassetid://112326668147130",

		[OnEvent("Click")] = function()
			(State.helpWidgetEnabled :: any):set(not (State.helpWidgetEnabled :: any):get())
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
					if not State.selectionConnection or not State.selectionConnection.Connected then
						State.selectionConnection = Selection.SelectionChanged:Connect(updateActiveRigFromSelection)
					end
					updateActiveRigFromSelection()
				else
					if State.selectionConnection then
						State.selectionConnection:Disconnect()
						State.selectionConnection = nil
					end
					cleanupAll()
				end
				return nil
			end) :: any
	)
	
	-- Handle help widget enabled/disabled
	table.insert(
		State.observers,
		(Observer(State.helpWidgetEnabled :: any) :: any):onChange(function(isEnabled: boolean)
				if helpButton and helpButton.Parent then
					helpButton:SetActive(isEnabled)
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
        local defaults = { "Player", "Rigging", "Blender Sync", "Tools", "More" }
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
    local sd = plugin:GetSetting("ShowDebugInfo")
    if typeof(sd) == "boolean" then State.showDebugInfo:set(sd) end
    
    -- Auto-connect to Blender if enabled
    if State.autoConnectToBlender:get() then
        task.spawn(function()
            task.wait(1) -- Wait a bit for everything to initialize
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
							PaddingBottom = UDim.new(0, 10),
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
										Size = UDim2.new(1, 0, 0, 120),
										BackgroundTransparency = 1,
										[Children] = {
											Button({
												Text = tabName,
												Size = UDim2.new(0, 120, 0, 18),
												Position = UDim2.fromScale(0.5, 0.5),
												AnchorPoint = Vector2.new(0.5, 0.5),
												Rotation = Computed(function()
													return if State.dockSide:get() == Enum.InitialDockState.Left then 90 else -90
												end),
												Activated = function()
													State.activeTab:set(tabName)
												end,
												BackgroundColorStyle = Computed(function()
													if State.activeTab:get() == tabName then
														return Enum.StudioStyleGuideColor.DiffFilePathBackground
													else
														return Enum.StudioStyleGuideColor.Button
													end
												end),
												[OnEvent("InputBegan")] = function(input)
													if input and input.UserInputType == Enum.UserInputType.MouseButton1 then
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
											Size = UDim2.new(0, 40, 0, 18),
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
						end) :: any,
					},
				}),
			},
		})
	end

	-- Create the main widget
	local function pluginWidget(tabChildren)
		return Widget({
			Id = game:GetService("HttpService"):GenerateGUID(),
			Name = "Blender Animations",
			InitialDockTo = State.dockSide:get(),
			InitialEnabled = false,
			ForceInitialEnabled = false,
			FloatingSize = Vector2.new(250, 600),
			MinimumSize = Vector2.new(250, 600),
			Enabled = State.widgetsEnabled,
			[OnChange("Enabled")] = function(isEnabled)
				(State.widgetsEnabled :: any):set(isEnabled)
				updateActiveRigFromSelection()
			end,
			[Children] = New("Frame")({
				Size = UDim2.fromScale(1, 1),
				BackgroundTransparency = 1,
				[Children] = {
					createTabsUI(),
					ScrollFrame({
						ZIndex = 1,
						Size = UDim2.new(1, -40, 1, 0),
						Position = Computed(function()
							return if State.dockSide:get() == Enum.InitialDockState.Left
								then UDim2.fromOffset(40, 0)
								else UDim2.fromOffset(0, 0)
						end),
						BackgroundTransparency = 1,
						AutomaticCanvasSize = Enum.AutomaticSize.Y,
						[Children] = Computed(function()
							local dynamicChildren = tabChildren:get()
							local allChildren = {
								_UIListLayout = New("UIListLayout")({
									SortOrder = Enum.SortOrder.LayoutOrder,
									Padding = UDim.new(0, 7),
								}),
								_UIPadding = New("UIPadding")({
									PaddingLeft = UDim.new(0, 5),
									PaddingRight = UDim.new(0, 5),
									PaddingBottom = UDim.new(0, 10),
									PaddingTop = UDim.new(0, 10),
								}),
							}
							for i, child in ipairs(dynamicChildren) do
								allChildren["child" .. i] = child
							end
							return allChildren
						end),
				}),
			},
			}),
		})
	end

	-- Create the main widget with tab content
	pluginWidget(Computed(function()
		local tab = State.activeTab:get()
		if tab == "Player" then
			return PlayerTab.create(services) :: any
		elseif tab == "Rigging" then
			return RiggingTab.create(services) :: any
		elseif tab == "Blender Sync" then
			return BlenderSyncTab.create(services) :: any
		elseif tab == "Tools" then
			return ToolsTab.create(services) :: any
		elseif tab == "More" then
			return MoreTab.create(services) :: any
		end
		return {}
	end))
	
	-- Create the help widget
	local _helpWidget = Widget({
		Id = game:GetService("HttpService"):GenerateGUID(),
		Name = "IMPORTANT READ ME!!!",
		InitialDockTo = Enum.InitialDockState.Float,
		InitialEnabled = false,
		ForceInitialEnabled = false,
		FloatingSize = Vector2.new(400, 500),
		MinimumSize = Vector2.new(400, 500),
		Enabled = State.helpWidgetEnabled,
		[OnChange("Enabled")] = function(isEnabled)
			(State.helpWidgetEnabled :: any):set(isEnabled)
		end,
		[Children] = {
			New("UIPadding")({
				PaddingLeft = UDim.new(0, 4),
				PaddingRight = UDim.new(0, 16),
				PaddingTop = UDim.new(0, 16),
				PaddingBottom = UDim.new(0, 16),
			}),
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 8),
			}),
			VerticalCollapsibleSection({
				Text = "MAJOR UPDATE AVAILABLE!",
				Collapsed = false,
				LayoutOrder = 2,
				[Children] = {
					New("Frame")({
						Size = UDim2.new(1, 0, 0, 64),
						BackgroundTransparency = 1,
						LayoutOrder = 1,
						[Children] = {
							New("ImageLabel")({
								Size = UDim2.new(0, 64, 0, 64),
								Position = UDim2.new(0.5, 0, 0.5, 0),
								AnchorPoint = Vector2.new(0.5, 0.5),
								BackgroundTransparency = 1,
								Image = "rbxassetid://92189642379919",
							}),
						},
					}),
					Label({
						LayoutOrder = 1,
						Text = "Please download the new Blender addon from Blender or Github to use this plugin. There has been a major update as you can probably tell. It is way more stable, fixes nearly all of the previous bugs, and has a lot of new features you will enjoy. This addon will continue to be free forever. I strongly recommend you update, and I hope you like the new logo. If you still wish to use clipboard and file export, you can still do so by enabling them in the More tab, HOWEVER the Server Sync has additional features that you would really miss out on. Also the new addon supports up to Blender 5.0+, godspeed my fellow animators. \n\n —Cautioned",
						TextWrapped = true,
					}),
					TextInput({
						LayoutOrder = 2,
						Text = "https://extensions.blender.org/approval-queue/roblox-animations-importer-exporter/",
					}),
					TextInput({
						LayoutOrder = 3,
						Text = "https://github.com/Cautioned/Blender-Animations-Plugin/releases",
					}),
					TextInput({
						LayoutOrder = 4,
						Text = "Copy and paste the URL into your browser to download the addon.",
					}),
				},
			}),
		},
	})
end

-- Always listen for selection changes
table.insert(State.connections, Selection.SelectionChanged:Connect(updateActiveRigFromSelection))



