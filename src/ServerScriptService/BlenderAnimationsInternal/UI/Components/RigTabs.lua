--!native
--!strict
--!optimize 2

-- Horizontal tab strip for switching between active rigs.
-- Supports drag-to-reorder (matching vertical sidebar tabs).
-- Each tab shows rig name + a close (×) button.

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Computed = Fusion.Computed
local Observer = Fusion.Observer

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local RIG_TAB_HEIGHT = 28
local TAB_GAP = 4

local RigTabs = {}

-- Build the horizontal rig-tab bar with drag-to-reorder support.
-- Always provides an explicit route into multi-rig mode via the "+" button.
function RigTabs.create(services: any)
	-- Dragging state
	local draggedTab = Fusion.Value(nil :: Instance?)
	local dropIndex = Fusion.Value(nil :: number?)
	local newTabHovered = Fusion.Value(false)

	-- Tab list container (built before the bar so it can be referenced)
	local tabList = New("ScrollingFrame")({
		Name = "TabList",
		Size = UDim2.new(1, 0, 1, 0),
		BackgroundTransparency = 1,
		BorderSizePixel = 0,
		AutomaticCanvasSize = Enum.AutomaticSize.X,
		CanvasSize = UDim2.new(),
		ScrollBarThickness = 4,
		ScrollingDirection = Enum.ScrollingDirection.X,
		[Children] = {
			New("UIListLayout")({
				FillDirection = Enum.FillDirection.Horizontal,
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, TAB_GAP),
				VerticalAlignment = Enum.VerticalAlignment.Center,
			}),
		},
	})

	local function rebuildTabs()
		local order = State.rigTabOrder:get()

		-- Clear old tabs (keep UIListLayout)
		for _, child in ipairs(tabList:GetChildren()) do
			if child:IsA("GuiObject") and child.Name ~= "UIListLayout" then
				child:Destroy()
			end
		end

		-- Only real sessions are tabs. Selection mode is state, not a fake tab.
		local entries: { Instance? } = {}
		for _, rig in ipairs(order) do
			table.insert(entries, rig)
		end

		local function makeDropIndicator(index: number)
			New("Frame")({
				Size = UDim2.new(0, 3, 1, -8),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
				BorderSizePixel = 0,
				Parent = tabList,
				LayoutOrder = index - 1,
				Visible = Computed(function()
					return dropIndex:get() == index
				end),
			})
		end

		for i = 1, #entries do
			local rigInst = entries[i]
			makeDropIndicator(i)

			local rigName = (rigInst :: Instance).Name
			local isHovered = Fusion.Value(false)
			local isActive = Computed(function()
				return State.activeSessionRig:get() == rigInst
			end)

			New("Frame")({
				Name = "RigTabWrapper_" .. rigName,
				Parent = tabList,
				LayoutOrder = i,
				Size = UDim2.new(0, 0, 1, 0),
				AutomaticSize = Enum.AutomaticSize.X,
				BackgroundColor3 = themeProvider:GetColor(Computed(function()
					if isActive:get() then
						return Enum.StudioStyleGuideColor.MainBackground
					end
					return Enum.StudioStyleGuideColor.Button
				end)),
				BorderSizePixel = 0,
				ZIndex = Computed(function()
					return if isActive:get() then 3 else 1
				end),
				[Children] = {
					New("UICorner")({
						CornerRadius = UDim.new(0, 8),
					}),
					New("UIListLayout")({
						FillDirection = Enum.FillDirection.Horizontal,
						SortOrder = Enum.SortOrder.LayoutOrder,
						Padding = UDim.new(0, 2),
						VerticalAlignment = Enum.VerticalAlignment.Center,
					}),
					New("TextButton")({
						Name = "TabButton",
						Text = rigName,
						Size = UDim2.new(0, 0, 1, -4),
						AutomaticSize = Enum.AutomaticSize.X,
						BackgroundTransparency = 1,
						BorderSizePixel = 0,
						Font = themeProvider:GetFont("Default"),
						TextSize = 14,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
						TextXAlignment = Enum.TextXAlignment.Left,
						AutoButtonColor = false,
						ZIndex = 4,
						[Children] = {
							New("UIPadding")({
								PaddingLeft = UDim.new(0, 10),
								PaddingRight = UDim.new(0, 2),
							}),
						},
						[OnEvent("InputBegan")] = function(input)
							if input and input.UserInputType == Enum.UserInputType.MouseButton1 then
								RigTabs._switchToRig(services, rigInst)
								draggedTab:set(rigInst)
							end
						end,
						[OnEvent("InputEnded")] = function(input)
							if input and input.UserInputType == Enum.UserInputType.MouseButton1 then
								local dropAt = dropIndex:get()
								if draggedTab:get() and dropAt then
									local currentOrder = table.clone(State.rigTabOrder:get())
									local dragIdx: number? = nil
									for j, r in ipairs(currentOrder) do
										if r == draggedTab:get() then
											dragIdx = j
											break
										end
									end
									if dragIdx then
										local moved = table.remove(currentOrder, dragIdx)
										local target = if dragIdx < dropAt then dropAt - 1 else dropAt
										table.insert(currentOrder, target, moved)
										State.rigTabOrder:set(currentOrder)
									end
								end
								draggedTab:set(nil)
								dropIndex:set(nil)
							end
						end,
					}),
					New("TextButton")({
						Name = "CloseButton",
						Text = utf8.char(215),
						Size = UDim2.new(0, 20, 1, -4),
						BackgroundTransparency = 1,
						BorderSizePixel = 0,
						Font = themeProvider:GetFont("Default"),
						TextSize = 16,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
						TextTransparency = Computed(function()
							return if isActive:get() or isHovered:get() then 0.2 else 1
						end),
						TextXAlignment = Enum.TextXAlignment.Center,
						TextYAlignment = Enum.TextYAlignment.Center,
						AutoButtonColor = false,
						ZIndex = 3,
						[OnEvent("Activated")] = function()
							RigTabs._closeRigTab(services, rigInst)
						end,
					}),
				},
				[OnEvent("MouseEnter")] = function()
					isHovered:set(true)
					if draggedTab:get() and draggedTab:get() ~= rigInst then
						dropIndex:set(i)
					end
				end,
				[OnEvent("MouseLeave")] = function()
					isHovered:set(false)
					if dropIndex:get() == i then
						dropIndex:set(nil)
					end
				end,
			})
		end

		makeDropIndicator(#entries + 1)

		-- Chrome-style new-tab control: compact until hover, highlighted while
		-- Studio selection is being used to add another rig.
		New("Frame")({
			Parent = tabList,
			LayoutOrder = #entries + 2,
			Size = UDim2.new(0, 28, 0, 24),
			BackgroundTransparency = 1,
			[Children] = {
				New("TextButton")({
					Name = "NewRigTabButton",
					Text = "+",
					AnchorPoint = Vector2.new(0.5, 0.5),
					Position = UDim2.fromScale(0.5, 0.5),
					Size = UDim2.fromOffset(22, 22),
					BackgroundColor3 = themeProvider:GetColor(Computed(function()
						if State.isAwaitingRigSelection:get() then
							return Enum.StudioStyleGuideColor.CheckedFieldBackground
						end
						return if newTabHovered:get()
							then Enum.StudioStyleGuideColor.Button
							else Enum.StudioStyleGuideColor.MainBackground
					end)),
					BorderSizePixel = 0,
					Font = themeProvider:GetFont("SemiBold"),
					TextSize = 19,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
					AutoButtonColor = false,
					[Children] = {
						New("UICorner")({ CornerRadius = UDim.new(0, 6) }),
					},
					[OnEvent("MouseEnter")] = function()
						newTabHovered:set(true)
					end,
					[OnEvent("MouseLeave")] = function()
						newTabHovered:set(false)
					end,
					[OnEvent("Activated")] = function()
						State.isAwaitingRigSelection:set(not State.isAwaitingRigSelection:get())
					end,
				}),
			},
		})

		New("TextLabel")({
			Parent = tabList,
			LayoutOrder = #entries + 3,
			Size = UDim2.new(0, 0, 1, 0),
			AutomaticSize = Enum.AutomaticSize.X,
			BackgroundTransparency = 1,
			Text = "select another rig",
			Font = themeProvider:GetFont("Default"),
			TextSize = 13,
			TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.SubText),
			Visible = State.isAwaitingRigSelection,
		})
	end

	-- Watch the manager-owned tab order and explicit selection mode.
	table.insert(
		State.observers,
		Observer(Computed(function()
			return { State.rigTabOrder:get(), State.isAwaitingRigSelection:get() }
		end)):onChange(function()
			rebuildTabs()
		end)
	)

	-- Initial build
	rebuildTabs()

	return New("Frame")({
		Name = "RigTabsBar",
		Size = UDim2.new(1, 0, 0, RIG_TAB_HEIGHT),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground),
		ClipsDescendants = false,
		[Children] = {
			New("UIListLayout")({
				FillDirection = Enum.FillDirection.Horizontal,
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, TAB_GAP),
				VerticalAlignment = Enum.VerticalAlignment.Center,
			}),
			New("UIPadding")({
				PaddingLeft = UDim.new(0, 4),
				PaddingRight = UDim.new(0, 4),
				PaddingTop = UDim.new(0, 4),
				PaddingBottom = UDim.new(0, 4),
			}),

			-- Divider line at bottom
			New("Frame")({
				Name = "RigTabsDivider",
				AnchorPoint = Vector2.new(0, 1),
				Position = UDim2.new(0, 0, 1, 0),
				Size = UDim2.new(1, 0, 0, 1),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
				BorderSizePixel = 0,
				ZIndex = 2,
				LayoutOrder = 999,
			}),

			tabList,
		},
	})
end

-- Switch active session to the given rig, saving current state first.
-- Does NOT stop/start playback — playback is global across all rigs.
function RigTabs._switchToRig(services: any, rigInst: Instance)
	if State.activeSessionRig:get() == rigInst then
		return
	end

	local manager = State.rigSessionManager
	if manager then
		manager:activate(rigInst)
	end
end

-- Reset State fields to represent a freshly selected rig with no animation.
-- Playback fields are left alone — they are global.
function RigTabs._resetRigState(state: any, rigInst: Instance?)
	state.activeRigModel = rigInst
	state.activeAnimator = nil
	state.activeRig = nil
	if rigInst then
		state.rigModelName:set(rigInst.Name)
		state.activeRigExists:set(true)
		state.rigScale:set(rigInst:GetScale())
		state.lastKnownRigModel = rigInst
	else
		state.rigModelName:set("No Rig Selected")
		state.activeRigExists:set(false)
		state.rigScale:set(1)
		state.lastKnownRigModel = nil
	end
	state.animationData = nil
	state.animationDirty:set(false)
	state.currentKeyframeSequence = nil
	state.keyframeNames:set({})
	state.keyframeStats:set({ count = 0, totalDuration = 0 })
	state.savedAnimations:set({})
	state.selectedSavedAnim:set(nil)
	state.boneWeights:set({})
	state.exportBoneWeights:set({})
	state.activeWarnings:set({})
	state.selectedPriority:set("Action")
	state.animationName = "KeyframeSequence"
	state.stopSpeed:set(2)
	state.setRigOrigin:set(true)
	state.scaleFactor:set(1)
	state.mirrorAnimationEnabled:set(false)
	state.speedEnabled:set(false)
	state.speedMultiplier:set(1)
	state.resampleEnabled:set(false)
	state.resampleFps:set(24)
	state.simplifierEnabled:set(false)
	state.simplifierStrength:set(15)
	state.animationModifierStack:set({})
	state.uniqueNames:set(true)
end

-- Close a rig tab: save state, remove from order, stop this rig's track.
-- If we drop to 0 real rigs (only pending slots), fully reset.
function RigTabs._closeRigTab(services: any, rigInst: Instance)
	local playbackService = services.playbackService

	-- Stop and remove this specific rig's track
	if playbackService._rigTracks and playbackService._rigTracks[rigInst] then
		local track = playbackService._rigTracks[rigInst]
		pcall(function()
			track:AdjustSpeed(0)
		end)
		pcall(function()
			track:Stop(0)
		end)
		pcall(function()
			track:Destroy()
		end)
		playbackService._rigTracks[rigInst] = nil
	end

	-- Remove from tab order
	local order = State.rigTabOrder:get()
	local newOrder = {}
	for _, r in ipairs(order) do
		if r ~= rigInst then
			table.insert(newOrder, r)
		end
	end
	State.rigTabOrder:set(newOrder)
	State.rigSessionManager:remove(rigInst)

	-- If we closed the active rig, switch to another if available
	if State.activeSessionRig:get() == rigInst then
		if #newOrder > 0 then
			-- Do not let _switchToRig snapshot the tab we just deleted.
			State.activeSessionRig:set(nil)
			State.rigSessionManager:activate(newOrder[1])
		elseif State.isAwaitingRigSelection:get() then
			-- Only pending slots remain — keep multi-rig open, clear active
			State.activeSessionRig:set(nil)
			RigTabs._resetRigState(State, nil)
		else
			-- Back to single-rig mode
			State.activeSessionRig:set(nil)
			RigTabs._resetRigState(State, nil)
		end
	end
end

-- Add a rig to the tab bar if not already present.
-- Returns true if this rig was newly added.
function RigTabs.addRigTab(rigInst: Instance): boolean
	local order = State.rigTabOrder:get()
	for _, r in ipairs(order) do
		if r == rigInst then
			return false -- already present
		end
	end

	local newOrder = {}
	for _, r in ipairs(order) do
		table.insert(newOrder, r)
	end
	table.insert(newOrder, rigInst)
	State.rigTabOrder:set(newOrder)
	return true
end

-- Remove a rig tab (called when a rig is deleted from workspace).
function RigTabs.removeRigTab(services: any, rigInst: Instance)
	RigTabs._closeRigTab(services, rigInst)
end

return RigTabs
