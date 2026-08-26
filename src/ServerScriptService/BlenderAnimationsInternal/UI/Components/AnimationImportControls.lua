--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local OnChange = Fusion.OnChange
local Value = Fusion.Value
local Computed = Fusion.Computed
local Tween = Fusion.Tween
local Spring = Fusion.Spring

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Checkbox = require(StudioComponents.Checkbox)
local Label = require(StudioComponents.Label)
local MainButton = require(StudioComponents.MainButton)
local TextInput = require(StudioComponents.TextInput)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)

local AnimationImportControls = {}

local function createLegacyImportSection(services: any, layoutOrder: number, selectedImportMode: any)
	local importHint = Value("")

	return New("Frame")({
		Name = "LegacyImport",
		LayoutOrder = layoutOrder,
		Size = UDim2.new(1, 0, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundTransparency = 1,
		Visible = Computed(function()
			return selectedImportMode:get() == "Legacy Import"
				and (State.enableFileExport:get() or State.enableClipboardExport:get())
		end),
		[Children] = {
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 7),
			}),
			MainButton({
				Text = "Import Animation from Clipboard",
				Size = UDim2.new(1, 0, 0, 30),
				Enabled = Computed(function()
					return State.activeRigExists:get() and State.enableClipboardExport:get()
				end),
				Activated = function(): nil
					services.playbackService:stopRigTrack(State.activeSessionRig:get())
					local importScriptText = "Paste the animation data below this line"

					services.exportManager:clearMetaParts()
					if State.importScript then
						State.importScript:Destroy()
					end
					local plugin = script:FindFirstAncestorWhichIsA("Plugin")
					State.importScript = Instance.new("Script", game.Workspace)
					assert(State.importScript)
					State.importScript.Archivable = false
					State.importScript.Source = "-- " .. importScriptText .. "\n"
					if plugin then
						plugin:OpenScript(State.importScript, 2)
					end
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
					return nil
				end,
				[OnEvent("MouseEnter")] = function()
					importHint:set("Opens a script editor. Paste animation data from the clipboard to import.")
				end,
				[OnEvent("MouseLeave")] = function()
					importHint:set("")
				end,
			}) :: any,
			MainButton({
				Text = "Import Animation from File(s)",
				Size = UDim2.new(1, 0, 0, 30),
				Enabled = Computed(function()
					return State.activeRigExists:get() and State.enableFileExport:get()
				end),
				Activated = function(): nil
					services.animationManager:importAnimationsBulk()
					return nil
				end,
				[OnEvent("MouseEnter")] = function()
					importHint:set("Opens a file dialog to import multiple .rbxanim files at once.")
				end,
				[OnEvent("MouseLeave")] = function()
					importHint:set("")
				end,
			}) :: any,
			SharedComponents.AnimatedHintLabel({
				Text = importHint,
				LayoutOrder = 3,
				ClipsDescendants = true,
				Size = UDim2.new(1, 0, 0, 0),
				TextWrapped = true,
				Visible = true,
				TextTransparency = 0,
			}),
		},
	})
end

local function createBlenderSyncSection(services: any, layoutOrder: number, selectedImportMode: any)
	local activeHint = Value("")

	return New("Frame")({
		Name = "BlenderSyncImport",
		LayoutOrder = layoutOrder,
		Size = UDim2.new(1, 0, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundTransparency = 1,
		Visible = Computed(function()
			return selectedImportMode:get() == "Blender Sync"
		end),
		[Children] = {
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 7),
			}),
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
				CanvasSize = UDim2.new(0, 0, 0, 0),
				AutomaticCanvasSize = Enum.AutomaticSize.Y,
				ScrollBarThickness = 4,
				ScrollBarImageTransparency = 0.5,
				Visible = Computed(function()
					return State.isServerConnected:get()
				end),
				[Children] = Computed(function()
					local armatures = State.availableArmatures:get()
					local selected = State.selectedArmature:get()
					local elements = {}

					table.insert(elements, New("UIListLayout")({
						Padding = UDim.new(0, 4),
						SortOrder = Enum.SortOrder.LayoutOrder,
						HorizontalAlignment = Enum.HorizontalAlignment.Center,
					}))
					table.insert(elements, New("UIPadding")({
						PaddingTop = UDim.new(0, 4),
						PaddingBottom = UDim.new(0, 4),
						PaddingLeft = UDim.new(0, 4),
						PaddingRight = UDim.new(0, 4),
					}))

					if #(armatures :: any) == 0 then
						table.insert(elements, New("Frame")({
							Size = UDim2.new(0.95, -2, 0, 50),
							BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Button),
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
									TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText) :: any,
								}),
							},
						}))
						return elements
					end

					for i, armature in ipairs(armatures :: any) do
						local isSelected = selected and (selected :: any).name == (armature :: any).name
						local isHovering = Value(false)
						local isPressed = Value(false)
						local rowScale = Tween(Computed(function()
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
						end), TweenInfo.new(0.12, Enum.EasingStyle.Quad, Enum.EasingDirection.Out))

						table.insert(elements, New("Frame")({
							Size = UDim2.new(0.95, -2, 0, 50),
							BackgroundColor3 = themeProvider:GetColor(Computed(function()
								return isSelected and Enum.StudioStyleGuideColor.DiffFilePathBackground
									or Enum.StudioStyleGuideColor.Button
							end)),
							BorderSizePixel = 0,
							LayoutOrder = i,
							[Children] = {
								New("UIScale")({
									Scale = rowScale,
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
									Text = string.format("%d bones", (armature :: any).num_bones),
									Size = UDim2.new(1, -10, 0.4, 0),
									Position = UDim2.new(0, 5, 0.6, 0),
									TextXAlignment = Enum.TextXAlignment.Left,
									TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText) :: any,
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
						}))
					end
					return elements
				end, Fusion.cleanup),
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
					if not State.isServerConnected:get() then
						activeHint:set("Connect to Blender first.")
					elseif State.selectedArmature:get() == nil then
						activeHint:set("Select an armature from Blender first.")
					else
						activeHint:set("Imports the current animation from the selected armature in Blender.")
					end
				end,
				[OnEvent("MouseLeave")] = function()
					activeHint:set("")
				end,
			}) :: any,
			Button({
				Text = "Export Animation to Blender",
				Size = UDim2.new(1, 0, 0, 30),
				LayoutOrder = 7,
				Enabled = Computed(function()
					return State.animationLength:get() > 0
				end),
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
					if State.animationLength:get() <= 0 then
						activeHint:set("Load or play an animation first.")
					else
						activeHint:set("Exports the currently playing animation to Blender.")
					end
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
					if not State.isServerConnected:get() then
						activeHint:set("Connect to Blender first.")
					else
						activeHint:set("Refreshes the list of available armatures from Blender.")
					end
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
					if not State.isServerConnected:get() then
						activeHint:set("Connect to Blender first.")
					elseif State.selectedArmature:get() == nil then
						activeHint:set("Select an armature from Blender first.")
					else
						activeHint:set("Automatically syncs animation changes from Blender in real-time.")
					end
				end,
				[OnEvent("MouseLeave")] = function()
					activeHint:set("")
				end,
			}) :: any,
			SharedComponents.AnimatedHintLabel({
				Text = activeHint,
				LayoutOrder = 10,
				Size = UDim2.new(1, 0, 0, 0),
				TextWrapped = true,
				ClipsDescendants = true,
				Visible = true,
				TextTransparency = 0,
			}),
		},
	}) :: any
end

function AnimationImportControls.create(services: any, layoutOrder: number?)
	local selectedImportMode = Value("Legacy Import")

	local function createImportTabButton(mode: string, order: number)
		local isHovering = Value(false)
		local isPressed = Value(false)
		-- smoother microanimation: spring-based y offset for subtle lift
		local tabYOffset = Spring(Computed(function()
			if State.reducedMotion:get() then
				return 0
			end
			if isPressed:get() then
				return 1
			end
			if selectedImportMode:get() == mode then
				return -1.25
			end
			if isHovering:get() then
				return -0.6
			end
			return 0
		end), 35, 0.75)

		local displayText = Computed(function()
			if mode ~= "Blender Sync" then
				return mode
			end
			return if State.isServerConnected:get()
				then "Blender Sync - Connected"
				else "Blender Sync - Offline"
		end)

		-- animated scale for hover/press
		local scaleSpring = Spring(Computed(function()
			if State.reducedMotion:get() then
				return 1
			end
			if isPressed:get() then
				return 0.985
			end
			if selectedImportMode:get() == mode then
				return 1.02
			end
			if isHovering:get() then
				return 1.01
			end
			return 1
		end), 25, 0.9)

		-- underline width is instant, no animation
		local underlineWidth = Computed(function()
			return selectedImportMode:get() == mode and 1 or 0
		end)

		local hoverTransparency = Tween(Computed(function()
			if selectedImportMode:get() == mode or isHovering:get() then
				return 0
			end
			return 1
		end), TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out))

		return New("Frame")({
			LayoutOrder = order,
			Size = UDim2.new(0.5, -3, 1, 0),
			BackgroundTransparency = 1,
			[Children] = {
				New("TextButton")({
					Text = displayText,
					Size = UDim2.fromScale(1, 1),
					Position = Computed(function()
						return UDim2.fromOffset(0, tabYOffset:get())
					end),
					BackgroundColor3 = themeProvider:GetColor(Computed(function()
						if selectedImportMode:get() == mode or isHovering:get() then
							return Enum.StudioStyleGuideColor.Button
						end
						return Enum.StudioStyleGuideColor.MainBackground
					end)),
					BackgroundTransparency = hoverTransparency,
					BorderSizePixel = 0,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
					Font = Enum.Font.SourceSansBold,
					TextSize = 13,
					TextXAlignment = Enum.TextXAlignment.Left,
					TextWrapped = true,
					TextTransparency = Computed(function()
						if selectedImportMode:get() == mode or isHovering:get() then
							return 0
						end
						return 0.08
					end),
					TextTruncate = Enum.TextTruncate.AtEnd,
					[Children] = {
						New("UIScale")({
							Scale = scaleSpring:get(),
						}),
						New("UICorner")({
							CornerRadius = UDim.new(0, 4),
						}),
						New("Frame")({
							Name = "SelectedUnderline",
							AnchorPoint = Vector2.new(0, 1),
							Position = UDim2.new(0, 0, 1, 0),
							Size = Computed(function()
								return UDim2.new(underlineWidth:get(), 0, 0, 2)
							end),
							BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
							BorderSizePixel = 0,
							Visible = Computed(function()
								return selectedImportMode:get() == mode
							end),
						}),
					},
					AutoButtonColor = false,
					Active = true,
					[OnEvent("InputBegan")] = function(input)
						if input.UserInputType == Enum.UserInputType.MouseMovement then
							isHovering:set(true)
						elseif input.UserInputType == Enum.UserInputType.MouseButton1 then
							isPressed:set(true)
						end
					end,
					[OnEvent("InputEnded")] = function(input)
						if input.UserInputType == Enum.UserInputType.MouseMovement then
							isHovering:set(false)
						elseif input.UserInputType == Enum.UserInputType.MouseButton1 then
							isPressed:set(false)
						end
					end,
					[OnEvent("Activated")] = function()
						selectedImportMode:set(mode)
					end,
				}),
			},
		})
	end

	return New("Frame")({
		Name = "AnimationImportControls",
		LayoutOrder = layoutOrder or 1,
		Size = UDim2.new(1, 0, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundTransparency = 1,
		[Children] = {
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 7),
			}),
			New("Frame")({
				Name = "ImportModeTabs",
				LayoutOrder = 1,
				Size = UDim2.new(1, 0, 0, 34),
				BackgroundTransparency = 1,
				[Children] = {
					New("UIListLayout")({
						FillDirection = Enum.FillDirection.Horizontal,
						SortOrder = Enum.SortOrder.LayoutOrder,
						Padding = UDim.new(0, 6),
					}),
					createImportTabButton("Legacy Import", 1),
					createImportTabButton("Blender Sync", 2),
				},
			}),
			createLegacyImportSection(services, 2, selectedImportMode),
			createBlenderSyncSection(services, 3, selectedImportMode),
		},
	})
end

return AnimationImportControls
