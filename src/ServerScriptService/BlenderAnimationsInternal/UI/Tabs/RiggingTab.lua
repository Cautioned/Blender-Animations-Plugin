--!native
--!strict
--!optimize 2
local ServerScriptService = game:GetService("ServerScriptService")


local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local _OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed
local Observer = Fusion.Observer

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Label = require(StudioComponents.Label)
local Button = require(StudioComponents.Button)
local Checkbox = require(StudioComponents.Checkbox)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = require(script.Parent.Parent.SharedComponents)
local ExportBoneToggles = require(script.Parent.Parent.Components.ExportBoneToggles)

local RiggingTab = {}

-- Create a ViewportFrame that shows a 3D preview of a model/part.
-- `instanceValue` is a Fusion Value<Instance?>.
local function createPreviewSlot(
	instanceValue: any,
	nameValue: any,
	layoutOrder: number,
	pickText: string,
	onPick: () -> (),
	onClear: (() -> ())?,
	hintLabel: string
)
	local viewportCamera = New("Camera")({
		FieldOfView = 40,
	})

	-- When the instance changes, clone it into the viewport and frame it
	local viewportChildren = Value({} :: { Instance })

	-- Spin state
	local spinCenter = Vector3.zero
	local spinDist = 5
	local spinHeight = 0.5
	local spinAngle = 0
	local spinConn: RBXScriptConnection? = nil
	local lastPreviewedInst: Instance? = nil
	local refreshScheduled = false

	local function refreshPreview()
		-- clear old children
		local inst = instanceValue:get()

		-- Skip if the actual instance hasn't changed
		if inst == lastPreviewedInst then
			return
		end
		lastPreviewedInst = inst

		viewportChildren:set({})
		if not inst then
			if spinConn then
				spinConn:Disconnect()
				spinConn = nil
			end
			return
		end

		-- clone into viewport
		local ok, cloned = pcall(function()
			local wasArchivable = inst.Archivable
			inst.Archivable = true
			local c = inst:Clone()
			inst.Archivable = wasArchivable
			return c
		end)
		if not ok or not cloned then return end

		-- figure out bounding box for camera framing
		local model: Model
		if cloned:IsA("Model") then
			model = cloned :: Model
		else
			model = Instance.new("Model")
			cloned.Parent = model
		end

		local cf, size = model:GetBoundingBox()
		local maxDim = math.max(size.X, size.Y, size.Z, 1)
		local dist = maxDim * 1.8

		-- Use the root part's front face so the camera starts facing the front
		local rootPart = model.PrimaryPart
		local frontDir: Vector3
		if rootPart then
			frontDir = rootPart.CFrame.LookVector
		else
			frontDir = cf.LookVector
		end

		-- Initial angle from front direction
		spinCenter = cf.Position
		spinDist = dist
		spinHeight = dist * 0.15
		spinAngle = math.atan2(frontDir.X, frontDir.Z)

		local camPos = spinCenter + Vector3.new(
			math.sin(spinAngle) * spinDist,
			spinHeight,
			math.cos(spinAngle) * spinDist
		)
		viewportCamera.CFrame = CFrame.lookAt(camPos, spinCenter)

		viewportChildren:set({ model })

		-- Start slow spin — update at a low rate to avoid per-frame viewport re-renders
		if spinConn then spinConn:Disconnect() end
		local spinAccum = 0
		local SPIN_INTERVAL = 0.1  -- update camera ~10 times/sec, not every frame
		spinConn = game:GetService("RunService").Heartbeat:Connect(function(dt)
			spinAngle = spinAngle + dt * 0.4  -- ~0.4 rad/s, full rotation in ~16s
			spinAccum = spinAccum + dt
			if spinAccum < SPIN_INTERVAL then
				return
			end
			spinAccum = spinAccum - SPIN_INTERVAL
			local pos = spinCenter + Vector3.new(
				math.sin(spinAngle) * spinDist,
				spinHeight,
				math.cos(spinAngle) * spinDist
			)
			viewportCamera.CFrame = CFrame.lookAt(pos, spinCenter)
		end)
	end

	-- Debounce refresh so back-to-back reactive updates (e.g. rigModelName + activeRigExists)
	-- only clone once instead of twice.
	local function scheduleRefresh()
		if refreshScheduled then
			return
		end
		refreshScheduled = true
		task.defer(function()
			refreshScheduled = false
			refreshPreview()
		end)
	end

	Observer(instanceValue):onChange(scheduleRefresh)
	task.defer(refreshPreview) -- initial

	local clearButton = onClear and New("TextButton")({
		Text = "x",
		Size = UDim2.fromOffset(18, 18),
		Position = UDim2.new(1, -2, 0, 2),
		AnchorPoint = Vector2.new(1, 0),
		BackgroundColor3 = Color3.fromRGB(180, 50, 50),
		TextColor3 = Color3.new(1, 1, 1),
		Font = Enum.Font.SourceSansBold,
		TextSize = 14,
		BorderSizePixel = 0,
		ZIndex = 3,
		Visible = Computed(function()
			return instanceValue:get() ~= nil
		end),
		[New("UICorner")] = nil,
		[Children] = {
			New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
		},
		[OnEvent("Activated")] = onClear,
	}) or nil

	return New("Frame")({
		LayoutOrder = layoutOrder,
		Size = UDim2.new(1, 0, 0, 90),
		BackgroundTransparency = 1,
		[Children] = {
			New("UIListLayout")({
				FillDirection = Enum.FillDirection.Horizontal,
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 8),
				VerticalAlignment = Enum.VerticalAlignment.Center,
			}),
			-- viewport thumbnail
			New("Frame")({
				LayoutOrder = 1,
				Size = UDim2.fromOffset(80, 80),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground),
				BorderSizePixel = 1,
				BorderColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBorder),
				ClipsDescendants = true,
				[Children] = {
					New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
					New("ViewportFrame")({
						Size = UDim2.fromScale(1, 1),
						BackgroundTransparency = 1,
						CurrentCamera = viewportCamera,
						[Children] = Computed(function()
							local items = viewportChildren:get()
							local out = { viewportCamera } :: { Instance }
							for _, item in ipairs(items) do
								table.insert(out, item)
							end
							return out
						end, Fusion.doNothing),
					}),
					-- empty state icon
					New("TextLabel")({
						Size = UDim2.fromScale(1, 1),
						BackgroundTransparency = 1,
						Text = "?",
						TextSize = 28,
						Font = Enum.Font.SourceSans,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText),
						Visible = Computed(function()
							return instanceValue:get() == nil
						end),
					}),
					clearButton,
				},
			}),
			-- info + button
			New("Frame")({
				LayoutOrder = 2,
				Size = UDim2.new(1, -88, 1, 0),
				BackgroundTransparency = 1,
				[Children] = {
					New("UIListLayout")({
						SortOrder = Enum.SortOrder.LayoutOrder,
						Padding = UDim.new(0, 2),
						VerticalAlignment = Enum.VerticalAlignment.Center,
					}),
					Label({
						LayoutOrder = 1,
						Text = Computed(function()
							return hintLabel
						end),
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText),
						TextSize = 11,
					}) :: any,
					Label({
						LayoutOrder = 2,
						Text = nameValue,
						Font = Enum.Font.SourceSansBold,
						TextSize = 14,
						TextTruncate = Enum.TextTruncate.AtEnd,
					}) :: any,
					Button({
						LayoutOrder = 3,
						Text = pickText,
						Size = UDim2.new(1, 0, 0, 22),
						Activated = onPick,
					}) :: any,
				},
			}),
		},
	})
end

function RiggingTab.create(services: any)
	local activeHint = Value("")
	local weaponHint = Value("")

	-- Rig preview uses the lastKnownRigModel / activeRigModel.
	-- Depend on BOTH activeRigExists AND rigModelName (both reactive Values)
	-- so the Computed re-fires on every rig selection / deselection.
	local rigPreviewValue = Computed(function()
		local _ = State.activeRigExists:get()
		local _ = State.rigModelName:get()
		return (State.activeRigModel or State.lastKnownRigModel) :: Instance?
	end, Fusion.doNothing)
	-- Use the actual instance name so the label stays correct even when
	-- activeRigModel is nil but lastKnownRigModel is still shown.
	local rigPreviewName = Computed(function()
		local _ = State.activeRigExists:get()
		local _ = State.rigModelName:get()
		local inst = State.activeRigModel or State.lastKnownRigModel
		if inst then
			return inst.Name
		end
		return "No Rig Selected"
	end)

	-- Re-detect connection when rig changes
	Observer(State.activeRigExists):onChange(function()
		if State.selectedWeapon:get() then
			services.exportManager:detectWeaponConnection()
		end
	end)

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
				Checkbox({
					Value = State.exportWelds,
					Text = "Export Welds (experimental)",
					OnChange = function(newValue: boolean): nil
						State.exportWelds:set(newValue)
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
		VerticalCollapsibleSection({
			Text = "Export Weapon / Accessory",
			Collapsed = true,
			LayoutOrder = 2,
			[Children] = {
				-- Rig preview slot (read-only, auto-populated)
				createPreviewSlot(
					rigPreviewValue,
					rigPreviewName,
					1,
					"Select in Explorer",
					function()
						local rig = State.activeRigModel or State.lastKnownRigModel
						if rig then
							game.Selection:Set({ rig })
						end
					end,
					nil,
					"RIG"
				),
				-- Weapon preview slot
				createPreviewSlot(
					State.selectedWeapon,
					State.selectedWeaponName,
					2,
					"Pick from Selection",
					function()
						services.exportManager:pickWeapon()
					end,
					function()
						services.exportManager:clearWeapon()
					end,
					"WEAPON"
				),
				-- Connection status
				New("Frame")({
					LayoutOrder = 3,
					Size = UDim2.new(1, 0, 0, 24),
					BackgroundTransparency = 1,
					[Children] = {
						New("UIPadding")({
							PaddingLeft = UDim.new(0, 4),
						}),
						Label({
							Text = Computed(function()
								local status = State.weaponConnectionStatus:get()
								if status == "" then
									return "Pick a weapon to check connection"
								end
								return status
							end),
							TextColor3 = Computed(function()
								local status = State.weaponConnectionStatus:get()
							if status:find("^[Vv]") or status:find(utf8.char(0x2713)) then
								return Color3.fromRGB(80, 200, 80)
							elseif status:find(utf8.char(0x26A0)) then
									return Color3.fromRGB(220, 160, 40)
								end
								return Color3.fromRGB(140, 140, 140)
							end),
							TextSize = 12,
							TextWrapped = true,
							Size = UDim2.new(1, 0, 1, 0),
						}) :: any,
					},
				}),
				-- Separator
				New("Frame")({
					LayoutOrder = 4,
					Size = UDim2.new(1, 0, 0, 1),
					BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
					BorderSizePixel = 0,
				}),
				-- Export button
				Button({
					LayoutOrder = 5,
					Text = "Export Weapon",
					Size = UDim2.new(1, 0, 0, 32),
					Enabled = Computed(function()
						local weapon = State.selectedWeapon:get()
						local rig = State.activeRigModel or State.lastKnownRigModel
						return weapon ~= nil and rig ~= nil
					end),
					Activated = function(): nil
						services.exportManager:exportWeapon()
						return nil
					end,
					[OnEvent("MouseEnter")] = function()
						weaponHint:set("Exports the slotted weapon with Motor6D hierarchy and connection data to the rig. Requires v2.4.0+ of the Blender add-on.")
					end,
					[OnEvent("MouseLeave")] = function()
						weaponHint:set("")
					end,
				}) :: any,
				Button({
					LayoutOrder = 6,
					Text = "Clean Meta Parts",
					Size = UDim2.new(1, 0, 0, 24),
					Activated = function(): nil
						services.exportManager:clearMetaParts()
						return nil
					end,
				}) :: any,
				SharedComponents.AnimatedHintLabel({
					Text = weaponHint,
					LayoutOrder = 7,
					Size = UDim2.new(1, 0, 0, 0),
					TextWrapped = true,
					ClipsDescendants = true,
					Visible = true,
					TextTransparency = 0,
				}),
			},
		}),
		ExportBoneToggles.create(services, 3),
	}
end

return RiggingTab
