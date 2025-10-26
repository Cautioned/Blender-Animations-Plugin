--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local _New = Fusion.New
local Children = Fusion.Children
local _OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local _Computed = Fusion.Computed

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Checkbox = require(StudioComponents.Checkbox)
local Label = require(StudioComponents.Label)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local _themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)

-- Donation supporters data: {userId = color} -- username fetched dynamically via Players:GetNameFromUserIdAsync
local DONATION_SUPPORTERS = {
    [1505918452] = "#9e4aff", -- JPlayHD
    [51636134] = "#d63838", -- AltiWyre
    [1418691708] = "#14acc7", -- jasper_creations
    [3414432341] = "#9da4bd", -- egor77
    [1388391899] = "#aed5f2", -- SlussBat
    [376669425] = "#87d68b", -- Phirsts
	[712807502] = "#ff708d", -- Tsuwukiz
	[117931886] = "#eba8f7", -- raddoob
	[1645075123] = "#f2b161", -- roffcat
	[25461577] = "#ed7b87", -- 144hertz
	[25637687] = "#fa9c50", -- TB_Glitch
	
}

local MoreTab = {}

-- Cache for generated thank you text
local _cachedThankYouText = nil
local _thankYouTextLoaded = false

-- Function to generate thank you text from supporters data (internal)
local function _generateThankYouText(): string
    -- Safely attempt to generate the thank you text with comprehensive error handling
    local success, result = pcall(function()
        local supportersText = "Thank you to all of those who have supported me in the development of this plugin, special shoutout to "

        local supporterEntries = {}

        -- Safely get Players service
        local Players
        local playersSuccess, playersResult = pcall(function()
            return game:GetService("Players")
        end)

        if not playersSuccess then
            warn("Failed to get Players service: " .. tostring(playersResult))
            -- Fallback: show supporters without usernames
            for userId, color in pairs(DONATION_SUPPORTERS) do
                table.insert(supporterEntries, string.format("<font color='%s'>Supporter %s</font>", color, tostring(userId)))
            end
            table.insert(supporterEntries, "and everyone else who has supported me!")
            return supportersText .. table.concat(supporterEntries, ", ") .. "."
        end

        Players = playersResult

        for userId, color in pairs(DONATION_SUPPORTERS) do
            local username = "Unknown"
            local apiSuccess, apiResult = pcall(function()
                return Players:GetNameFromUserIdAsync(userId)
            end)

            if apiSuccess and apiResult then
                username = apiResult
            else
                warn("Failed to get username for user ID " .. tostring(userId) .. ": " .. tostring(apiResult))
                -- Fallback to User + ID for display
                username = "User" .. tostring(userId)
            end

            -- Safely format the supporter entry
            local formatSuccess, formattedEntry = pcall(function()
                return string.format("<font color='%s'>%s</font>", color, username)
            end)

            if formatSuccess then
                table.insert(supporterEntries, formattedEntry)
            else
                warn("Failed to format supporter entry for user ID " .. tostring(userId))
                table.insert(supporterEntries, string.format("<font color='%s'>Supporter %s</font>", color, tostring(userId)))
            end
        end

        -- Add "and everyone else who has supported me!" at the end
        table.insert(supporterEntries, "and everyone else who has supported me!")

        -- Safely concatenate the entries
        local concatSuccess, finalText = pcall(function()
            return supportersText .. table.concat(supporterEntries, ", ")
        end)

        if concatSuccess then
            return finalText
        else
            warn("Failed to concatenate supporter entries")
            return supportersText .. "our amazing supporters!"
        end
    end)

    if success then
        return result
    else
        warn("Critical error generating thank you text: " .. tostring(result))
        -- Ultimate fallback
        return "Thank you to all of those who have supported me in the development of this plugin!"
    end
end

-- Function to preload thank you text (call when support section is opened)
local function _preloadThankYouText(): nil
    if not _thankYouTextLoaded then
        local success, generatedText = pcall(_generateThankYouText)
        if success and generatedText then
            _cachedThankYouText = generatedText
            _thankYouTextLoaded = true
            print("Preloaded donation supporters text")
        else
            warn("Failed to preload donation supporters text: " .. tostring(generatedText))
            -- Still mark as loaded to avoid repeated attempts
            _thankYouTextLoaded = true
        end
    end
    return nil
end

-- Function to get cached thank you text or generate it
local function getThankYouText(): string
    if _cachedThankYouText and _thankYouTextLoaded then
        return _cachedThankYouText
    end

    -- Generate the text if not cached
    local success, generatedText = pcall(_generateThankYouText)
    if success and generatedText then
        _cachedThankYouText = generatedText
        _thankYouTextLoaded = true
        return generatedText
    else
        warn("Failed to generate thank you text: " .. tostring(generatedText))
        -- Return a safe fallback
        local fallbackText = "Thank you to all of those who have supported me in the development of this plugin!"
        _cachedThankYouText = fallbackText
        _thankYouTextLoaded = true
        return fallbackText
    end
end


function MoreTab.create(services: any)
	local activeHint = Value("")
	local thankYouText = Value("Loading supporters...") -- Initial placeholder text

	return {
		VerticalCollapsibleSection({
			Text = "Export Options",
			Collapsed = false,
			LayoutOrder = 1,
			[Children] = {
                Checkbox({
					Value = State.enableFileExport,
					Text = "Enable File Export",
					LayoutOrder = 1,
					OnChange = function(enabled: boolean): nil
                        State.enableFileExport:set(enabled)
                        services.plugin:SetSetting("EnableFileExport", enabled)
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Allows exporting animations to files from Blender.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}),
                Checkbox({
					Value = State.enableClipboardExport,
					Text = "Enable Clipboard Export",
					LayoutOrder = 2,
					OnChange = function(enabled: boolean): nil
                        State.enableClipboardExport:set(enabled)
                        services.plugin:SetSetting("EnableClipboardExport", enabled)
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Allows exporting animations to clipboard from Blender.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}),
			},
		}) :: any,
		VerticalCollapsibleSection({
			Text = "Live Sync Options",
			Collapsed = false,
			LayoutOrder = 2,
			[Children] = {
                Checkbox({
					Value = State.enableLiveSync,
					Text = "Enable Live Sync",
					LayoutOrder = 1,
					OnChange = function(enabled: boolean): nil
						State.enableLiveSync:set(enabled)
                        services.plugin:SetSetting("EnableLiveSync", enabled)
						if not enabled then
							services.blenderSyncManager:stopLiveSyncing()
						end
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Automatically syncs animations from Blender to Roblox in real-time.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}),
                Checkbox({
					Value = State.autoConnectToBlender,
					Text = "Auto-connect to Blender",
					LayoutOrder = 2,
					OnChange = function(enabled: boolean): nil
                        State.autoConnectToBlender:set(enabled)
                        services.plugin:SetSetting("AutoConnectToBlender", enabled)
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Automatically connects to Blender when the plugin starts.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}),
			},
		}) :: any,
		SharedComponents.AnimatedHintLabel({
			Text = activeHint,
			LayoutOrder = 4,
			Size = UDim2.new(1, 0, 0, 0),
			TextWrapped = true,
			ClipsDescendants = true,
			Visible = true,
			TextTransparency = 0,
			RichText = true,
		}),
		VerticalCollapsibleSection({
			Text = "About",
			Collapsed = false,
			LayoutOrder = 5,
			[Children] = {
				Fusion.New("Frame")({
					Size = UDim2.new(1, 0, 0, 64),
					BackgroundTransparency = 1,
					LayoutOrder = 1,
					[Children] = {
						Fusion.New("ImageLabel")({
							Size = UDim2.new(0, 64, 0, 64),
							Position = UDim2.new(0.5, 0, 0.5, 0),
							AnchorPoint = Vector2.new(0.5, 0.5),
							BackgroundTransparency = 1,
							Image = "rbxassetid://92189642379919",
						}),
					},
				}),
				Label{
					LayoutOrder = 2,
					Text = "To use this plugin, you need to have the Blender addon installed."
				},
				TextInput{
					LayoutOrder = 3,
					Text = "https://github.com/Cautioned/Blender-Animations-Plugin",
				},
				
				Label{
					LayoutOrder = 5,
					Text = "This plugin will always be free, to support development use any of the below links."
				},
				VerticalCollapsibleSection {
					Collapsed = true,
					Text = "Support Development",
					LayoutOrder = 6,
					OnCollapsedChanged = function(isExpanded: boolean)
						if isExpanded then
							-- Load the thank you text when section is expanded
							local text = getThankYouText()
							thankYouText:set(text)
						end
					end,
					[Children] = {
						TextInput{
							LayoutOrder = 1,
							Text = "https://ko-fi.com/cautioned",
						},
						TextInput{
							LayoutOrder = 2,
							Text = "https://www.roblox.com/games/12361360692/#!/store",
						},
						Label{
							LayoutOrder = 3,
							Text = thankYouText,
							RichText = true
						},
						Label{
							LayoutOrder = 4,
							Text = "Like the plugin? Leave a review on the Roblox Store!"
						},
						Label{
							LayoutOrder = 5,
							Text = "⚠",
							RichText = true
						},
		
						Label{
							LayoutOrder = 6,
							Text = "Maintained by Cautioned @CAUTlONED.",
							RichText = true
						},
					}
				} :: any,
			},
		}) :: any,
	}
end

return MoreTab
