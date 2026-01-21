--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)
local Plugin = script:FindFirstAncestorWhichIsA("Plugin")

local New = Fusion.New
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed
local Spring = Fusion.Spring

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Checkbox = require(StudioComponents.Checkbox)
local Label = require(StudioComponents.Label)
local MainButton = require(StudioComponents.MainButton)
local TextInput = require(StudioComponents.TextInput)
local Dropdown = require(StudioComponents.Dropdown)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)
local PlaybackControls = require(script.Parent.Parent.Components.PlaybackControls)
local BoneToggles = require(script.Parent.Parent.Components.BoneToggles)

local BlenderSyncTab = {}

function BlenderSyncTab.create(services: any)
	local activeHint = Value("")
	local legacyImportHint = Value("")
	local saveUploadHint = Value("")


	return {
		VerticalCollapsibleSection({
			Text = "Blender Connection",
			Collapsed = false,
			[Children] = {
				Label({
					Text = "Connect to Blender addon via server. Use the same port on both the Blender Addon and this plugin.",
					LayoutOrder = 1,
				}),
				TextInput({
					PlaceholderText = "31337",
					Text = Computed(function()
						return tostring(State.serverPort:get())
					end),
					LayoutOrder = 2,
					[OnChange("Text")] = function(newPort)
						local port = tonumber(newPort)
						if port and port >= 1024 and port <= 65535 then
							State.serverPort:set(port)
						else
							services.rigManager:addWarning("Invalid port number. Please enter a number between 1024 and 65535.")
						end
					end,
				}),
				MainButton({
					Text = Computed(function()
						return State.isServerConnected:get() and "Disconnect" or "Connect"
					end),
					Size = UDim2.new(1, 0, 0, 30),
					LayoutOrder = 3,
					Activated = function(): nil
						services.blenderSyncManager:toggleServerConnection()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Connects to or disconnects from the Blender addon server.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Label({
					Text = Computed(function()
						return "Status: " .. State.serverStatus:get()
					end),
					LayoutOrder = 4,
					TextColor3 = themeProvider:GetColor(Computed(function()
						if State.serverStatus:get() == "Connected" then
							return Enum.StudioStyleGuideColor.ScriptInformation
						else
							return Enum.StudioStyleGuideColor.ErrorText
						end
					end)),
				}),
				New("ScrollingFrame")({
					Size = UDim2.new(1, 0, 0, 150),
					LayoutOrder = 5,
					BackgroundTransparency = 1,
					CanvasSize = UDim2.new(0.95, 0, 0, 0),
					AutomaticCanvasSize = Enum.AutomaticSize.Y,
					ScrollBarThickness = 0,
					Visible = Computed(function()
						return State.isServerConnected:get()
					end),
					[Children] = Computed(function()
						local armatures = State.availableArmatures:get()
						local selected = State.selectedArmature:get()
						local elements = {}
						
						table.insert(
							elements,
							New("UIListLayout")({
								Padding = UDim.new(0, 4),
								SortOrder = Enum.SortOrder.LayoutOrder,
								HorizontalAlignment = Enum.HorizontalAlignment.Center,
							})
						)
						table.insert(
							elements,
							New("UIPadding")({
								PaddingTop = UDim.new(0, 4),
								PaddingBottom = UDim.new(0, 4),
								PaddingLeft = UDim.new(0, 4),
								PaddingRight = UDim.new(0, 4),
							})
						)

						if #(armatures :: any) == 0 then
							table.insert(
								elements,
								New("Frame")({
									Size = UDim2.new(0.95, -2, 0, 50),
									BackgroundColor3 = themeProvider:GetColor(
										Enum.StudioStyleGuideColor.Button
									),
									BorderSizePixel = 0,
									LayoutOrder = 1,
									[Children] = {
										New("UICorner")({
											CornerRadius = UDim.new(0, 4),
										}),
										Label({
											Text = "No armatures found in Blender.",
											Size = UDim2.new(1, 0, 1, 0),
											BackgroundTransparency = 1,
											TextColor3 = themeProvider:GetColor(
												Enum.StudioStyleGuideColor.DimmedText
											) :: any,
										}),
									},
								})
							)
							return elements
						end

						for i, armature in ipairs(armatures :: any) do
							local isSelected = selected and (selected :: any).name == (armature :: any).name
							local isHovering = Value(false)
							local isPressed = Value(false)

							table.insert(
								elements,
								New("Frame")({
									Size = UDim2.new(0.95, -2, 0, 50),
									BackgroundColor3 = themeProvider:GetColor(Computed(function()
										return isSelected and Enum.StudioStyleGuideColor.DiffFilePathBackground
											or Enum.StudioStyleGuideColor.Button
									end)),
									BorderSizePixel = 0,
									LayoutOrder = i,
									[Children] = {
										New("UIScale")({
											Scale = Spring(Computed(function()
												if State.reducedMotion:get() then
													return 1
												end
												if isPressed:get() then
													return 0.97
												end
												if isHovering:get() then
													return 1.02
												end
												return 1
											end), 25, 0.9),
										}),
										New("UICorner")({
											CornerRadius = UDim.new(0, 4),
										}),
										New("UIListLayout")({
											Padding = UDim.new(0.05, 4),
											FillDirection = Enum.FillDirection.Vertical,
											HorizontalAlignment = Enum.HorizontalAlignment.Left,
											VerticalAlignment = Enum.VerticalAlignment.Center,
										}),
										Label({
											Text = (armature :: any).name,
											Size = UDim2.new(1, -10, 0.6, 0),
											Position = UDim2.new(0, 5, 0, 0),
											TextXAlignment = Enum.TextXAlignment.Left,
											Font = Enum.Font.SourceSansBold,
										}),
										Label({
											Text = string.format(
												"%d bones",
												(armature :: any).num_bones
											),
											Size = UDim2.new(1, -10, 0.4, 0),
											Position = UDim2.new(0, 5, 0.6, 0),
											TextXAlignment = Enum.TextXAlignment.Left,
											TextColor3 = themeProvider:GetColor(
												Enum.StudioStyleGuideColor.DimmedText
											) :: any,
										}),
									},
									[OnEvent("MouseEnter")] = function()
										isHovering:set(true)
									end,
									[OnEvent("MouseLeave")] = function()
										isHovering:set(false)
										isPressed:set(false)
									end,
									[OnEvent("InputBegan")] = function(input)
										if input.UserInputType == Enum.UserInputType.MouseButton1 then
											isPressed:set(true)
											State.selectedArmature:set(armature)
										end
									end,
									[OnEvent("InputEnded")] = function(input)
										if input.UserInputType == Enum.UserInputType.MouseButton1 then
											isPressed:set(false)
										end
									end,
								})
							)
						end
						return elements
					end),
				}) :: any,
				Button({
					Text = "Import Animation from Blender",
					Size = UDim2.new(1, 0, 0, 30),
					LayoutOrder = 6,
					Enabled = Computed(function()
						return State.isServerConnected:get() and State.selectedArmature:get() ~= nil
					end),
					Activated = function(): nil
						State.loadingEnabled:set(true)
						local success = services.blenderSyncManager:importAnimationFromBlender()
						State.loadingEnabled:set(false)
						if not success then
							services.rigManager:addWarning("Failed to import animation from Blender")
						end
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Imports the current animation from the selected armature in Blender.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Export Animation to Blender",
					Size = UDim2.new(1, 0, 0, 30),
					LayoutOrder = 7,
					Enabled = true,
					Activated = function(): nil
						State.loadingEnabled:set(true)
						local success = services.blenderSyncManager:exportAnimationToBlender()
						State.loadingEnabled:set(false)
						if not success then
							services.rigManager:addWarning("Failed to export animation to Blender")
						end
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Exports the currently playing animation to Blender. You can load them in the player tab, place the animation inside your AnimSaves.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Refresh Armatures",
					Size = UDim2.new(1, 0, 0, 30),
					LayoutOrder = 8,
					Enabled = Computed(function()
						return State.isServerConnected:get()
					end),
					Activated = function(): nil
						services.blenderSyncManager:updateAvailableArmatures()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Refreshes the list of available armatures from Blender.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Checkbox({
					Text = "Live Sync Animation",
					Value = State.liveSyncEnabled,
					LayoutOrder = 9,
					Visible = Computed(function()
						return State.enableLiveSync:get()
					end),
					OnChange = function(newValue)
						State.liveSyncEnabled:set(newValue)
						if newValue then
							services.blenderSyncManager:startLiveSyncing()
						else
							services.blenderSyncManager:stopLiveSyncing()
						end
					end,
					Enabled = Computed(function()
						return State.isServerConnected:get() and State.selectedArmature:get() ~= nil
					end),
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Automatically syncs animation changes from Blender in real-time.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Save Animation to Rig",
					Size = UDim2.new(1, 0, 0, 30),
					LayoutOrder = 11,
					Enabled = Computed(function()
						return State.activeRigExists:get() and State.animationLength:get() > 0
					end),
					Activated = function(): nil
						services.animationManager:saveAnimationRig()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Saves the current animation as a KeyframeSequence inside the rig's AnimSaves folder.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Upload Animation to Roblox",
					Size = UDim2.new(1, 0, 0, 30),
					LayoutOrder = 12,
					Enabled = Computed(function()
						return State.activeRigExists:get() and State.animationLength:get() > 0
					end),
					Activated = function(): nil
						services.animationManager:uploadAnimation()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Uploads the current animation to your Roblox account.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				SharedComponents.AnimatedHintLabel({
					Text = activeHint,
					LayoutOrder = 13,
					Size = UDim2.new(1, 0, 0, 0),
					TextWrapped = true,
					ClipsDescendants = true,
					Visible = true,
					TextTransparency = 0,
				}),
				PlaybackControls.createPlaybackSection(services, 14) :: any,
				Label({
					Text = "Animation Name",
					LayoutOrder = 15,
				}),
				TextInput({
					PlaceholderText = "KeyframeSequence",
					Text = State.animationName,
					LayoutOrder = 16,
					[OnChange("Text")] = function(newText)
						if newText == "" then
							State.animationName = "KeyframeSequence"
						else
							State.animationName = newText
						end
					end,
				}),
				Checkbox({
					Value = State.uniqueNames,
					Text = "Keep Names Unique",
					LayoutOrder = 17,
					OnChange = function(uniqueState: boolean): nil
						State.uniqueNames:set(uniqueState)
						return nil
					end,
				}),
				Label({
					Text = "Animation Priority",
					LayoutOrder = 18,
				}),
				Dropdown({
					Size = UDim2.new(1, 0, 0, 25),
					LayoutOrder = 19,
					Value = State.selectedPriority,
					Options = State.animationPriorityOptions :: any,
					OnSelected = function(newItem: any): nil
						State.selectedPriority:set(newItem) -- Update the state based on selection
						return nil
					end,
				}),
				SharedComponents.AnimatedHintLabel({
					Text = saveUploadHint,
					LayoutOrder = 20,
					ClipsDescendants = true,
					Size = UDim2.new(1, 0, 0, 0),
					TextWrapped = true,
					Visible = true,
					TextTransparency = 0,
				}),
			},
		}),
		BoneToggles.create(services, 2),
		
	}
	
end

return BlenderSyncTab
