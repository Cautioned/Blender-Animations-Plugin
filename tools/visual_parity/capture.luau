-- CaptureService fallback; transfers only the generated test viewport to localhost.
assert(workspace:FindFirstChild("RBXParityFixtureMarker"), "Open a generated parity place")
local caseId = "__CASE_ID__"
local contentId
local camera=workspace.CurrentCamera
-- Allow the active frame and material resources to settle after a case change.
task.wait(3)
local capturedCf=camera.CFrame
local cameraCf={capturedCf:GetComponents()}
local fov=camera.FieldOfView
game:GetService("CaptureService"):CaptureScreenshot(function(id) contentId=id end)
local deadline=os.clock()+12
repeat task.wait(0.05) until contentId or os.clock()>deadline
assert(contentId,"CaptureService did not return a screenshot")
assert(camera.CFrame==capturedCf and camera.FieldOfView==fov,"Camera moved during capture; retry the reference")
local image=game:GetService("AssetService"):CreateEditableImageAsync(Content.fromUri(contentId))
local size=image.Size
local lighting=game:GetService("Lighting")
local http=game:GetService("HttpService")
local encoding=game:GetService("EncodingService")
local receipt
for y=0,size.Y-1,64 do
    local rows=math.min(64,size.Y-y)
    local pixels=image:ReadPixelsBuffer(Vector2.new(0,y),Vector2.new(size.X,rows))
    local payload={case=caseId,width=size.X,height=size.Y,chunk_y=y,chunk_height=rows,
        rgba_base64=buffer.tostring(encoding:Base64Encode(pixels)),
        camera_cf=cameraCf,fov=fov,capture_service="CaptureService",content_id=contentId,
        editor_quality=tostring(settings().Rendering.QualityLevel),
        lighting_style=tostring(lighting.LightingStyle),brightness=lighting.Brightness,
        exposure=lighting.ExposureCompensation,clock_time=lighting.ClockTime,
        viewport={camera.ViewportSize.X,camera.ViewportSize.Y}}
    receipt=http:JSONDecode(http:PostAsync("http://127.0.0.1:18765/capture",http:JSONEncode(payload),Enum.HttpContentType.ApplicationJson))
end
image:Destroy()
return {receipt=receipt,contentId=contentId,camera_cf=cameraCf,width=size.X,height=size.Y,fov=fov}
