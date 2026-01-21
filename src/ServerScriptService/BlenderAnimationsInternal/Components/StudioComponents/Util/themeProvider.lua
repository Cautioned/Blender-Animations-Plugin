local types = require(script.Parent.types)

type styleStyleGuideColor = Enum.StudioStyleGuideColor | types.StateObject<Enum.StudioStyleGuideColor>
type styleGuideModifier = Enum.StudioStyleGuideModifier | types.StateObject<Enum.StudioStyleGuideModifier>
type computedOrValue = types.Computed<Color3> | types.Value<Color3>

local Studio = settings().Studio
local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local unwrap = require(script.Parent.unwrap)

local Computed = Fusion.Computed
local Value = Fusion.Value

local currentTheme = {}
local themeProvider = {
	Theme = Value(Studio.Theme.Name),
	Fonts = {
		Default = Enum.Font.BuilderSans,
		SemiBold = Enum.Font.BuilderSansMedium,
		Bold = Enum.Font.BuilderSansBold,
		Black = Enum.Font.GothamBlack,
		Mono = Enum.Font.Code,
	},
	IsDark = Value(true),
}

function themeProvider:GetColor(studioStyleGuideColor: styleStyleGuideColor, studioStyleGuideModifier: styleGuideModifier?): any
	local hasState = (unwrap(studioStyleGuideModifier, false) ~= studioStyleGuideModifier) or (unwrap(studioStyleGuideColor, false) ~= studioStyleGuideColor)

	local function isCorrectType(value, enumType)
		local unwrapped = unwrap(value, false)
		local isState = unwrapped ~= value and unwrapped~=nil
		assert((value==nil or isState) or (typeof(value)=="EnumItem" and value.EnumType==enumType), "Incorrect type")
	end

	isCorrectType(studioStyleGuideColor, Enum.StudioStyleGuideColor)
	isCorrectType(studioStyleGuideModifier, Enum.StudioStyleGuideModifier)

	local unwrappedColor = unwrap(studioStyleGuideColor, false)
	local unwrappedModifier = unwrap(studioStyleGuideModifier, false)

	if not currentTheme[unwrappedColor] then
		currentTheme[unwrappedColor] = {}
	end

	local themeValue = (function()
		local styleGuideModifier = if unwrappedModifier~=nil then unwrappedModifier else Enum.StudioStyleGuideModifier.Default

		local existingValue = currentTheme[unwrappedColor][styleGuideModifier]
		if existingValue then
			return existingValue
		end

		local newThemeValue = Value(Studio.Theme:GetColor(unwrappedColor, styleGuideModifier))
		currentTheme[unwrappedColor][styleGuideModifier] = newThemeValue

		return newThemeValue
	end)()

	return if not hasState then themeValue else Computed(function()
		local currentColor = unwrap(studioStyleGuideColor)
		local currentModifier = unwrap(studioStyleGuideModifier)
		local currentValueState = self:GetColor(currentColor, currentModifier)
		return currentValueState:get()
	end)
end

function themeProvider:GetFont(fontName: (string | types.StateObject<string>)?): types.Computed<Enum.Font>
	return Computed(function()
		local givenFontName = unwrap(fontName)
		local fontToGet = self.Fonts.Default
		if givenFontName~=nil and self.Fonts[givenFontName] then
			fontToGet = self.Fonts[givenFontName]
		end
		return unwrap(fontToGet)
	end)
end

local function updateTheme()
	for studioStyleGuideColor, styleGuideModifiers: {Enum.StudioStyleGuideModifier} in pairs(currentTheme) do
		for studioStyleGuideModifier, valueState in pairs(styleGuideModifiers) do
			valueState:set(Studio.Theme:GetColor(studioStyleGuideColor, studioStyleGuideModifier))
		end
	end
	themeProvider.Theme:set(Studio.Theme.Name)

	local _,_,v = Studio.Theme:GetColor(Enum.StudioStyleGuideColor.MainBackground):ToHSV()
	themeProvider.IsDark:set(v<=0.6)
end

do
	local themeChangedConnection = Studio.ThemeChanged:Connect(updateTheme)
	updateTheme()

	Plugin.Unloading:Connect(function()
		themeChangedConnection:Disconnect()
		themeChangedConnection = nil
	end)
end

return themeProvider
