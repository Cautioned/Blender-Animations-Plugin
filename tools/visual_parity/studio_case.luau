-- Generated fixture controller. Run only inside a generated parity place.
local spec = game:GetService("HttpService"):JSONDecode([==[__SCENE_JSON__]==])
local workspace = game:GetService("Workspace")
local lighting = game:GetService("Lighting")
assert(workspace:FindFirstChild("RBXParityFixtureMarker"), "Open a generated parity place first")
local history = game:GetService("ServerStorage"):FindFirstChild("ParityFixtureHistory")
if not history then
    history = Instance.new("Folder")
    history.Name = "ParityFixtureHistory"
    history.Parent = game:GetService("ServerStorage")
end
-- Keep prior fixture geometry instead of deleting it. No user place is touched.
local previous = Instance.new("Folder")
previous.Name = "PreviousFixture"
previous.Parent = history
for _, item in workspace:GetChildren() do
    if item:IsA("BasePart") and item.ClassName ~= "Terrain" or item:IsA("Model") and item:GetAttribute("ParityOwned") then
        item.Parent = previous
    end
end
for _, effect in lighting:GetChildren() do
    if effect:IsA("PostEffect") then effect.Enabled = false end
end
local root = Instance.new("Model")
root.Name = "Parity_" .. spec.id
root:SetAttribute("ParityOwned", true)
root.Parent = workspace
local objects = {}
for index, item in spec.nodes do
    if item.class == "Workspace" then objects[index] = root
    elseif item.class == "Lighting" then objects[index] = lighting
    elseif item.class == "MaterialService" then objects[index] = game:GetService("MaterialService")
    elseif item.class == "Camera" then objects[index] = workspace.CurrentCamera
    else objects[index] = Instance.new(item.class) end
end
local enumTypes = {
    Material = Enum.Material, Shape = Enum.PartType, TopSurface = Enum.SurfaceType,
    BottomSurface = Enum.SurfaceType, Face = Enum.NormalId, CameraType = Enum.CameraType,
    LightingStyle = Enum.LightingStyle, Technology = Enum.Technology,
}
local function enumValue(kind, value)
    for _, item in kind:GetEnumItems() do if item.Value == value then return item end end
    error("Unknown enum value " .. tostring(value))
end
for index, item in spec.nodes do
    local target = objects[index]
    for property, pair in item.properties do
        local kind, value = pair[1], pair[2]
        if property ~= "Name" or item.class ~= "Workspace" and item.class ~= "Lighting" and item.class ~= "MaterialService" then
            if (property == "Use2022Materials" or property == "Use2022MaterialsXml") then
                -- RobloxScript-only: loaded from the fixture file, unavailable to plugins.
            elseif property ~= "Technology" then
                if kind == "CoordinateFrame" then target[property] = CFrame.new(table.unpack(value))
                elseif kind == "Vector3" then target[property] = Vector3.new(table.unpack(value))
                elseif kind == "Color3" then target[property] = Color3.new(table.unpack(value))
                elseif kind == "Color3uint8" then target.Color = Color3.fromRGB(table.unpack(value))
                elseif kind == "token" then target[property] = enumValue(assert(enumTypes[property],property), value)
                elseif kind == "Ref" then
                    if property == "CurrentCamera" then workspace.CurrentCamera = objects[value + 1]
                    else target[property] = objects[value + 1] end
                else target[property] = value end
            end
        end
    end
    if item.parent >= 0 and item.class ~= "Camera" then target.Parent = objects[item.parent + 1] end
end
local camera = workspace.CurrentCamera
camera.CameraType = Enum.CameraType.Scriptable
camera.CFrame = CFrame.new(table.unpack(spec.camera_cf))
camera.FieldOfView = spec.fov
game:GetService("Selection"):Set({})
local direction = lighting:GetSunDirection()
return {case=spec.id, root=root:GetFullName(), camera_cf={camera.CFrame:GetComponents()},
    width=camera.ViewportSize.X, height=camera.ViewportSize.Y, fov=camera.FieldOfView,
    style=tostring(lighting.LightingStyle), quality=lighting.PrioritizeLightingQuality,
    editor_quality=tostring(settings().Rendering.QualityLevel),
    material_generation="Loaded from fixture; runtime property restricted",
    brightness=lighting.Brightness, sun={direction.X,direction.Y,direction.Z}}
