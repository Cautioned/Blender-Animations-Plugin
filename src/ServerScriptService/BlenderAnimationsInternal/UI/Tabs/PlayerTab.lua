--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Checkbox = require(StudioComponents.Checkbox)
local Button = require(StudioComponents.Button)
local MainButton = require(StudioComponents.MainButton)
local Label = require(StudioComponents.Label)
local Dropdown = require(StudioComponents.Dropdown)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)
local PlaybackControls = require(script.Parent.Parent.Components.PlaybackControls)

local PlayerTab = {}

function PlayerTab.create(services: any)
	local importHint = Value("")
	local saveUploadHint = Value("")

	return {
		SharedComponents.createHeaderUI(),
		PlaybackControls.createPlaybackSection(services),
		VerticalCollapsibleSection({
			Text = "Legacy Import",
			Collapsed = false,
			LayoutOrder = 1,
			Visible = Computed(function()
				return State.enableFileExport:get() or State.enableClipboardExport:get()
			end),
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
                        local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
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
				Label({
					Text = "Using the Blender Sync tab is recommended for additional features, file and clipboard import will continue to be supported but may lack features in the future.",
					LayoutOrder = 4,
				}),
			},
		}),

		-- Playback section would go here (extracted to separate module)

		VerticalCollapsibleSection({
			Text = "Save/Upload",
			Collapsed = false,
			LayoutOrder = 3,
			[Children] = {
				VerticalCollapsibleSection({
					Text = "Saved Animations",
					Collapsed = false,
					[Children] = {

						New("ScrollingFrame")({
							Size = UDim2.new(1, 0, 0, 200),
							LayoutOrder = 2,
							BackgroundTransparency = 1,
							CanvasSize = UDim2.new(0.95, 0, 0, 0),
							AutomaticCanvasSize = Enum.AutomaticSize.Y,
							ScrollBarThickness = 0,
							[Children] = Computed(function()
								local anims = State.savedAnimations:get()
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
								for i, anim in ipairs(anims) do
									local selectedAnim = State.selectedSavedAnim:get()
									local isSelected = selectedAnim ~= nil
										and selectedAnim.instance == (anim :: any).instance

									table.insert(
										elements,
										New("Frame")({
											Size = UDim2.new(0.95, -2, 0, 30),
											BackgroundColor3 = themeProvider:GetColor(Computed(function()
												return isSelected and Enum.StudioStyleGuideColor.ButtonBorder
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
													Text = (anim :: any).name,
													Size = UDim2.new(1, -10, 1, 0),
													Position = UDim2.new(0, 5, 0, 0),
													TextXAlignment = Enum.TextXAlignment.Left,
													Font = Enum.Font.SourceSansBold,
												}),
											},
											[OnEvent("InputBegan")] = function(input)
												if input.UserInputType == Enum.UserInputType.MouseButton1 then
													State.selectedSavedAnim:set(anim)
													services.animationManager:playSavedAnimation(anim)
												end
											end,
										})
									)
								end

								if #anims == 0 then
									table.insert(
										elements,
										New("Frame")({
											Size = UDim2.new(1, -8, 0, 50),
											BackgroundColor3 = themeProvider:GetColor(
												Enum.StudioStyleGuideColor.Button
											),
											BorderSizePixel = 0,
											[Children] = {
												Label({
													Text = "No saved animations found.\nSave an animation to see it here.",
													Size = UDim2.new(1, 0, 1, 0),
													BackgroundTransparency = 1,
													TextColor3 = themeProvider:GetColor(
														Enum.StudioStyleGuideColor.DimmedText
													) :: any,
												}),
											},
										})
									)
								end

								return elements
							end),
						}),
					},
				}) :: any,
				Label({
					Text = "Animation Name",
					LayoutOrder = 0,
				}),
				TextInput({
					PlaceholderText = "KeyframeSequence",
					Text = State.animationName,
					LayoutOrder = 1,
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
					LayoutOrder = 2,
					OnChange = function(uniqueState: boolean): nil
						State.uniqueNames:set(uniqueState)
						return nil
					end,
				}),
				Label({
					Text = "Animation Priority",
					LayoutOrder = 3,
				}),
				Dropdown({
					Size = UDim2.new(1, 0, 0, 25),
					LayoutOrder = 4,
					Value = State.selectedPriority,
					Options = State.animationPriorityOptions :: any,
					OnSelected = function(newItem: any): nil
						State.selectedPriority:set(newItem) -- Update the state based on selection
						return nil
					end,
				}),
				Button({
					Text = "Upload Animation to Roblox",
					Size = UDim2.new(1, 0, 0, 30),
					Enabled = Computed(function()
						return State.activeRigExists:get()
					end),
					LayoutOrder = 5,
					Activated = function(): nil
						services.animationManager:uploadAnimation()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						saveUploadHint:set("Uploads the current animation to your Roblox account.")
					end,
					[OnEvent("MouseLeave")] = function()
						saveUploadHint:set("")
					end,
				}) :: any,
				Button({
					LayoutOrder = 6,
					Text = "Save Animation to Rig",
					Size = UDim2.new(1, 0, 0, 30),
					Enabled = Computed(function()
						return State.activeRigExists:get()
					end),
					Activated = function(): nil
						services.animationManager:saveAnimationRig()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						saveUploadHint:set("Saves the animation as a KeyframeSequence inside the rig model.")
					end,
					[OnEvent("MouseLeave")] = function()
						saveUploadHint:set("")
					end,
				}) :: any,
				SharedComponents.AnimatedHintLabel({
					Text = saveUploadHint,
					LayoutOrder = 7,
					ClipsDescendants = true,
					Size = UDim2.new(1, 0, 0, 0),
					TextWrapped = true,
					Visible = true,
					TextTransparency = 0,
				}),
			},
		}),
		VerticalCollapsibleSection({
			Text = "Bone Toggles",
			Collapsed = false,
			LayoutOrder = 3,
			Visible = State.activeRigExists,
			[Children] = Computed(function()
				local boneWeights = State.boneWeights:get()
				local boneToggles = {}

				for i, bone in ipairs(boneWeights) do
					-- Create indented text based on depth
					local indentText = string.rep("  ", bone.depth) .. bone.name
					table.insert(boneToggles, Checkbox({
						Value = bone.enabled,
						Text = indentText,
						LayoutOrder = i,
						OnChange = function(enabled: boolean)
							print("Bone toggle changed:", bone.name, "enabled:", enabled)
							-- Update the bone weight
							bone.enabled = enabled
							State.boneWeights:set(boneWeights) -- Trigger reactivity

							-- Update the rig animation if it exists
							if State.activeRig and State.activeRig.bones then
								-- Find the rig bone by name more reliably
								local rigBone = State.activeRig.bones[bone.name]
								if rigBone then
									rigBone.enabled = enabled
									print("Updated rig bone:", bone.name, "enabled:", enabled)
								else
									-- Fallback: search by part name
									for _, rb in pairs(State.activeRig.bones) do
										if rb.part.Name == bone.name then
											rb.enabled = enabled
											print("Updated rig bone (fallback):", bone.name, "enabled:", enabled)
											break
										end
									end
								end
								
								-- Reload the animation to see the effect immediately
								if services.playbackService then
									services.playbackService:stopAnimationAndDisconnect()
									services.playbackService:playCurrentAnimation(State.activeAnimator)
								end
							else
								print("No active rig or bones found")
							end
						end,
					}))
				end

				return boneToggles
			end),
		}),
	}
end

return PlayerTab
