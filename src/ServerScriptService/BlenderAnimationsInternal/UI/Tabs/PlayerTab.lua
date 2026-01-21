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
local Spring = Fusion.Spring

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
local BoneToggles = require(script.Parent.Parent.Components.BoneToggles)

local PlayerTab = {}

function PlayerTab.create(services: any)
	local importHint = Value("")
	local saveUploadHint = Value("")
	local previousAnimCount = Value(0)
	local newAnimIndices = Value({} :: { [number]: boolean })
	local rowStateCache: { [any]: { animProgress: any, isHovering: any, isPressed: any } } = {}

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
								local currentCount = #anims
								local prevCount = previousAnimCount:get()

								-- Track which animations are new
								if currentCount > prevCount then
									local newIndices = {}
									for i = prevCount + 1, currentCount do
										newIndices[i] = true
									end
									newAnimIndices:set(newIndices)
									previousAnimCount:set(currentCount)
									-- Clear new animation indicators asynchronously so rendering stays non-blocking
									task.delay(0.6, function()
										if previousAnimCount:get() == currentCount then
											newAnimIndices:set({})
										end
									end)
								elseif currentCount < prevCount then
									previousAnimCount:set(currentCount)
									newAnimIndices:set({})
								end

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
								local seenKeys = {}
								for i, anim in ipairs(anims) do
									local selectedAnim = State.selectedSavedAnim:get()
									local isSelected = selectedAnim ~= nil
										and selectedAnim.instance == (anim :: any).instance

									local isNew = newAnimIndices:get()[i] or false
									local cacheKey = (anim :: any).instance or anim
									seenKeys[cacheKey] = true
									local stateForRow = rowStateCache[cacheKey]
									if not stateForRow then
										stateForRow = {
											animProgress = Value(1),
											isHovering = Value(false),
											isPressed = Value(false),
										}
										rowStateCache[cacheKey] = stateForRow
									end
									local animProgress = stateForRow.animProgress
									local isHovering = stateForRow.isHovering
									local isPressed = stateForRow.isPressed

									if isNew and not State.reducedMotion:get() then
										animProgress:set(0.95)
										task.delay(0.06, function()
											animProgress:set(1)
										end)
									else
										animProgress:set(1)
									end

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
												New("UIScale")({
													Scale = Spring(
														Computed(function()
															local base = animProgress:get()
															if State.reducedMotion:get() then
																return base
															end
															if isPressed:get() then
																return base * 0.97
															end
															if isHovering:get() then
																return base * 1.02
															end
															return base
														end),
														25,
														0.9
													),
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
													Text = (anim :: any).name,
													Size = UDim2.new(1, -10, 1, 0),
													Position = UDim2.new(0, 5, 0, 0),
													TextXAlignment = Enum.TextXAlignment.Left,
													Font = Enum.Font.SourceSansBold,
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
													State.selectedSavedAnim:set(anim)
													services.animationManager:playSavedAnimation(anim)
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

								-- Trim cache entries for animations that were removed
								for key, _ in pairs(rowStateCache) do
									if not seenKeys[key] then
										rowStateCache[key] = nil
									end
								end

								table.insert(
									elements,
									(function()
										local importHover = Value(false)
										local importPressed = Value(false)
										return New("Frame")({
											Size = UDim2.new(0.95, -2, 0, 22),
											BackgroundTransparency = 1,
											LayoutOrder = #anims + 1,
											[Children] = {
												New("TextButton")({
													Text = "⤓ Import From Roblox ⤓",
													Size = UDim2.new(1, 0, 1, 0),
													BackgroundColor3 = themeProvider:GetColor(
														Enum.StudioStyleGuideColor.TableItem
													),
													TextColor3 = themeProvider:GetColor(
														Enum.StudioStyleGuideColor.MainText
													),
													BorderColor3 = themeProvider:GetColor(
														Enum.StudioStyleGuideColor.ButtonBorder
													),
													BorderSizePixel = 2,
													Font = Enum.Font.BuilderSansBold,
													TextSize = 12,
													[Children] = {
														New("UICorner")({
															CornerRadius = UDim.new(0, 4),
														}),
														New("UIScale")({
															Scale = Spring(
																Computed(function()
																	if State.reducedMotion:get() then
																		return 1
																	end
																	if importPressed:get() then
																		return 0.97
																	end
																	if importHover:get() then
																		return 1.02
																	end
																	return 1
																end),
																25,
																0.9
															),
														}),
													},
													[OnEvent("MouseEnter")] = function()
														importHover:set(true)
													end,
													[OnEvent("MouseLeave")] = function()
														importHover:set(false)
														importPressed:set(false)
													end,
													[OnEvent("InputBegan")] = function(input)
														if input.UserInputType == Enum.UserInputType.MouseButton1 then
															importPressed:set(true)
														end
													end,
													[OnEvent("InputEnded")] = function(input)
														if input.UserInputType == Enum.UserInputType.MouseButton1 then
															importPressed:set(false)
														end
													end,
													[OnEvent("Activated")] = function()
														services.animationManager:importAnimationsFromRoblox()
													end,
												}),
											},
										})
									end)()
								)

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
													TextXAlignment = Enum.TextXAlignment.Center,
													TextYAlignment = Enum.TextYAlignment.Center,
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
		BoneToggles.create(services, 3),
	}
end

return PlayerTab
