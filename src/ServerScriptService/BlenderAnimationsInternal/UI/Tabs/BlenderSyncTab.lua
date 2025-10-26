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

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Checkbox = require(StudioComponents.Checkbox)
local Label = require(StudioComponents.Label)
local MainButton = require(StudioComponents.MainButton)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)

local BlenderSyncTab = {}

function BlenderSyncTab.create(services: any)
	local activeHint = Value("")
	local legacyImportHint = Value("")

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
									[OnEvent("InputBegan")] = function(input)
										if input.UserInputType == Enum.UserInputType.MouseButton1 then
											State.selectedArmature:set(armature)
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
						activeHint:set("Exports the current animation to Blender.")
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
			},
		}),
		VerticalCollapsibleSection({
			Text = "Legacy Import",
			Collapsed = false,
			LayoutOrder = 1,
			[Children] = {
				MainButton({
					Text = "Import Animation from Clipboard",
					Size = UDim2.new(1, 0, 0, 30),
					Enabled = Computed(function()
						return State.activeRigExists:get() and State.enableClipboardExport:get()
					end),
					Activated = function(): nil
						services.playbackService:stopAnimationAndDisconnect()
						local importScriptText = "Paste the animation data below this line"

						services.exportManager:clearMetaParts()
						if State.importScript then
							State.importScript:Destroy()
						end
						State.importScript = Instance.new("Script", game.Workspace)
						assert(State.importScript)
						State.importScript.Archivable = false
                        State.importScript.Source = "-- " .. importScriptText .. "\n"
                        if Plugin then
                            Plugin:OpenScript(State.importScript, 2)
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
						legacyImportHint:set("Opens a script editor. Paste animation data from the clipboard to import.")
					end,
					[OnEvent("MouseLeave")] = function()
						legacyImportHint:set("")
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
						legacyImportHint:set("Opens a file dialog to import multiple .rbxanim files at once.")
					end,
					[OnEvent("MouseLeave")] = function()
						legacyImportHint:set("")
					end,
				}) :: any,
			},
		}),
	}
	
end

return BlenderSyncTab
