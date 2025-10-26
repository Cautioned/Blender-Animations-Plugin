--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed
local Children = Fusion.Children

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Label = require(StudioComponents.Label)
local Checkbox = require(StudioComponents.Checkbox)
local LimitedTextInput = require(StudioComponents.LimitedTextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)
local PlaybackControls = require(script.Parent.Parent.Components.PlaybackControls)
local CameraControls = require(script.Parent.Parent.Components.CameraControls)
local KeyframeNaming = require(script.Parent.Parent.Components.KeyframeNaming)

local ToolsTab = {}

function ToolsTab.create(services: any)
	local activeHint = Value("")

	local components = {}

	-- Safely create components
	local playbackSection = PlaybackControls.createPlaybackSection(services)
	if playbackSection then
		table.insert(components, playbackSection)
	end

	local cameraControls = CameraControls.createCameraControlsUI(services)
	if cameraControls then
		table.insert(components, cameraControls)
	end

	local keyframeNaming = KeyframeNaming.createKeyframeNamingUI(services, 3)
	if keyframeNaming then
		table.insert(components, keyframeNaming)
	end

	-- Add the Animation Modifiers section
	table.insert(
		components,
		VerticalCollapsibleSection({
			Text = "Animation Modifiers",
			Collapsed = false,
			LayoutOrder = 4,
			[Children] = {
				Label({
					Text = "Animation Resizer (Default: 1)",
					LayoutOrder = 1,
				}),
				LimitedTextInput({
					PlaceholderText = "1",
					Text = Computed(function()
						return tostring(State.scaleFactor:get())
					end),
					LayoutOrder = 2,
					GraphemeLimit = 8,
					[OnChange("Text")] = function(newScaleFactorText)
						local newScaleFactor = tonumber(newScaleFactorText)
						if newScaleFactor then
							if newScaleFactor and newScaleFactor > 0 then
								State.scaleFactor:set(newScaleFactor)
								if State.activeAnimator and services and services.playbackService then
									services.playbackService:playCurrentAnimation(State.activeAnimator)
								end
							end
						end
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set(
							"Resizes the animation by a given factor. Useful for scaling animations up or down."
						)
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				} :: any),
				Label({
					Text = Computed(function()
						local scaleFactor = State.scaleFactor:get()
						local rigScale = State.rigScale:get()
						return string.format("Scale Factor: %.2f | Model Scale: %.2f", scaleFactor, rigScale)
					end),
					LayoutOrder = 3,
				}),
				Label({
					Text = "More coming soon...",
					LayoutOrder = 4,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScriptComment) :: any,
				}),
				SharedComponents.AnimatedHintLabel({
					Text = activeHint,
					LayoutOrder = 5,
					Size = UDim2.new(1, 0, 0, 0),
					TextWrapped = true,
					ClipsDescendants = true,
					Visible = true,
					TextTransparency = 0,
				}),
			},
		}) :: any
	)

	-- Add the Bone Toggles section
	table.insert(
		components,
		VerticalCollapsibleSection({
			Text = "Bone Toggles",
			Collapsed = false,
			LayoutOrder = 5,
			Visible = State.activeRigExists,
			[Children] = Computed(function()
				local boneWeights = State.boneWeights:get()
				local boneToggles = {}

				for i, bone in ipairs(boneWeights) do
					-- Create indented text based on depth
					local indentText = string.rep("  ", bone.depth) .. bone.name
					table.insert(
						boneToggles,
						Checkbox({
							Value = bone.enabled,
							Text = indentText,
							LayoutOrder = i,
							OnChange = function(enabled: boolean)
								-- Update the bone weight
								bone.enabled = enabled
								State.boneWeights:set(boneWeights) -- Trigger reactivity

								-- Update the rig animation if it exists
								if State.activeRig and State.activeRig.bones then
									for _, rigBone in pairs(State.activeRig.bones) do
										if rigBone.part.Name == bone.name then
											rigBone.enabled = enabled
											break
										end
									end
								end
							end,
						})
					)
				end

				return boneToggles
			end),
		}) :: any
	)

	return components
end

return ToolsTab
