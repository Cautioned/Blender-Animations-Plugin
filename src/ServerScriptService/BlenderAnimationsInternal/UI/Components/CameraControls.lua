--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local Value = Fusion.Value
local Computed = Fusion.Computed

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Dropdown = require(StudioComponents.Dropdown)
local Label = require(StudioComponents.Label)
local Slider = require(StudioComponents.Slider)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)


local CameraControls = {}

function CameraControls.createCameraControlsUI(services: any)
	return VerticalCollapsibleSection({
		Text = "🎥 Camera Controls",
		Collapsed = not State.activeRigExists:get(),
		LayoutOrder = 2,
		[Children] = {
			Label({
				Text = "Select Part to Attach Camera",
				LayoutOrder = 1,
			}),
			New("Frame")({
				Size = UDim2.new(1, 0, 0, 25),
				LayoutOrder = 2,
				[Children] = {
					Dropdown({
						Options = services and services.cameraManager and services.cameraManager:getPartsList() or Value({}),
						OnSelected = function(newItem: any): nil
							if State.activeRigModel then
								State.selectedPart:set(State.activeRigModel:FindFirstChild(newItem))
							end
							return nil
						end,
					}),
				},
			}) :: any,
			New("Frame")({
				LayoutOrder = 3,
				Size = UDim2.new(1, 0, 0, 30),
				[Children] = {
					Button({
						Text = Computed(function()
							return State.isCameraAttached:get() and "🎥 Detach Camera" or "🎥 Attach Camera"
						end),
						Activated = function(): nil
							if services and services.cameraManager then
								if State.isCameraAttached:get() then
									services.cameraManager:detachCamera()
								else
									services.cameraManager:attachCameraToPart(State.selectedPart:get())
								end
							end
							return nil
						end,
						Enabled = Computed(function()
							return State.activeRigExists:get()
						end),
					}),
				},
			}) :: any,
			Label({
				LayoutOrder = 4,
				Text = "Adjust Field of View",
			}),
			Slider({
				LayoutOrder = 5,
				Size = UDim2.new(1, 0, 0, 20),
				Min = 10,
				Max = 120,
				Step = 1,
				Value = Computed(function()
					return State.fovValue:get()
				end),
				OnChange = function(value)
					State.fovValue:set(value)
					local camera = game.Workspace.CurrentCamera
					if camera then
						camera.FieldOfView = value
					end
				end,
				Enabled = Computed(function()
					return State.isCameraAttached:get()
				end),
			}) :: any,
			Label({
				LayoutOrder = 6,
				Text = Computed(function()
					return tostring(State.fovValue:get())
				end),
			}),
		},
	})
end

return CameraControls
