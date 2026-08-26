--!native
--!strict
--!optimize 2

local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local Computed = Fusion.Computed

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)
local CameraControls = require(script.Parent.Parent.Components.CameraControls)
local KeyframeNaming = require(script.Parent.Parent.Components.KeyframeNaming)
local BoneToggles = require(script.Parent.Parent.Components.BoneToggles)
local AnimationModifierStack = require(script.Parent.Parent.Components.AnimationModifierStack)

local ToolsTab = {}

local function createToolDivider(layoutOrder: number)
	return New("Frame")({
		LayoutOrder = layoutOrder,
		Size = UDim2.new(1, 0, 0, 1),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
		BorderSizePixel = 0,
	})
end

local function createAnimationModifiers(services: any, layoutOrder: number): Frame
	local mainBackground = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground)
	local containerBackground = Computed(function()
		local contrastTarget = if themeProvider.IsDark:get() then Color3.new(0, 0, 0) else Color3.new(1, 1, 1)
		return mainBackground:get():Lerp(contrastTarget, 0.1)
	end)

	return New("Frame")({
		Name = "AnimationModifiers",
		LayoutOrder = layoutOrder,
		Size = UDim2.new(1, 0, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundColor3 = containerBackground,
		BorderSizePixel = 0,
		[Children] = {
			New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
			New("UIStroke")({
				Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
				Transparency = 0.25,
			}),
			New("UIPadding")({
				PaddingTop = UDim.new(0, 6),
				PaddingRight = UDim.new(0, 6),
				PaddingBottom = UDim.new(0, 6),
				PaddingLeft = UDim.new(0, 6),
			}),
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 5),
			}),
			New("TextLabel")({
				LayoutOrder = 1,
				Size = UDim2.new(1, 0, 0, 22),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.HeaderSection),
				BackgroundTransparency = 0,
				Font = themeProvider:GetFont("Bold"),
				Text = "Animation Modifiers",
				TextSize = 13,
				TextXAlignment = Enum.TextXAlignment.Left,
				TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.BrightText),
				[Children] = {
					New("UICorner")({ CornerRadius = UDim.new(0, 3) }),
					New("UIStroke")({
						Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.ButtonBorder),
						Transparency = 0.2,
					}),
					New("UIPadding")({ PaddingLeft = UDim.new(0, 7) }),
				},
			}),
			New("Frame")({
				LayoutOrder = 2,
				Size = UDim2.new(1, 0, 0, 0),
				AutomaticSize = Enum.AutomaticSize.Y,
				BackgroundTransparency = 1,
				[Children] = AnimationModifierStack.create(services),
			}),
		},
	})
end

function ToolsTab.create(services: any)
	local components = {}

	local cameraControls = CameraControls.createCameraControlsUI(services)
	if cameraControls then
		table.insert(components, cameraControls)
	end

	local keyframeNaming = KeyframeNaming.createKeyframeNamingUI(services, 4)
	if keyframeNaming then
		table.insert(components, keyframeNaming)
		table.insert(components, createToolDivider(5))
	end

	table.insert(components, createAnimationModifiers(services, 6))

	-- divider between animation modifiers and bone toggles
	table.insert(components, createToolDivider(7))

	-- Add the Bone Toggles section
	table.insert(components, BoneToggles.create(services, 8) :: any)

	return components
end

return ToolsTab
