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
local Computed = Fusion.Computed

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Checkbox = require(StudioComponents.Checkbox)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local _themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)

local RiggingTab = {}

function RiggingTab.create(services: any)
	local activeHint = Value("")

	return {
		SharedComponents.createHeaderUI(),
		VerticalCollapsibleSection({
			Text = "Export Rig",
			Collapsed = false,
			LayoutOrder = 1,
			[Children] = {
				Button({
					Text = "Sync Bones (experimental)",
					Size = UDim2.new(1, 0, 0, 30),
					Enabled = Computed(function()
						return State.activeRigExists:get()
					end),
					Activated = function(): nil
						-- create missing bones/motors from blender armature
					services.rigManager:syncBones(services.blenderSyncManager)
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Made a new bone? Attached a weapon to your rig in blender? This will create the bone in studio.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Export Rig",
					Size = UDim2.new(1, 0, 0, 30),
					Enabled = Computed(function()
						return State.activeRigExists:get()
					end),
					Activated = function(): nil
						services.exportManager:exportRig()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Exports the rig by deleting the humanoid. This may have issues with textures and meshes, but the rig will usually rebuild easily in Blender.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Export Rig [Legacy]",
					Size = UDim2.new(1, 0, 0, 30),
					Enabled = Computed(function()
						return State.activeRigExists:get()
					end),
					Activated = function(): nil
						services.exportManager:exportRigLegacy()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set(
							"Exports the rig with a legacy method. Roblox randomizes mesh/part names when rigs include a humanoid, so you will have to rename them in Blender and constrain them manually.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Button({
					Text = "Clean Meta Parts",
					Size = UDim2.new(1, 0, 0, 30),
					Activated = function(): nil
						services.exportManager:clearMetaParts()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						activeHint:set("Removes leftover metadata parts from a previous export. This cannot be done automatically.")
					end,
					[OnEvent("MouseLeave")] = function()
						activeHint:set("")
					end,
				}) :: any,
				Checkbox({
					Value = State.setRigOrigin,
					Text = "Center Rig to Origin for Export",
					OnChange = function(newValue: boolean): nil
						State.setRigOrigin:set(newValue)
						return nil
					end,
				}) :: any,
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
		}),
	}
end

return RiggingTab
