-- Native Animator verification for test_rbxm_rebuild.py exports.
-- R6/R15 use clones of the open place templates; Bone cases use a temporary
-- MeshPart/Bone hierarchy built from the parsed binary fixture's rest frames.
return function(Rig, template, case)
    local model, sequence, animation, track
    local report = {id=case.id, samples=0, failures=0, maxPosition=0, maxAngle=0}
    local ok, result = xpcall(function()
        if template then
            model = template:Clone()
        else
            model = Instance.new("Model")
            local root = case.metadata.rig
            local body = Instance.new("MeshPart")
            body.Name = root.jname
            body.CFrame = CFrame.new(table.unpack(root.transform))
            body.Parent = model
            model.PrimaryPart = body
            local function addBones(node, parent)
                for _, child in node.children or {} do
                    local bone = Instance.new("Bone")
                    bone.Name = child.jname
                    bone.CFrame = CFrame.new(table.unpack(child.jointtransform0))
                    bone.Parent = parent
                    addBones(child, bone)
                end
            end
            addBones(root, body)
            Instance.new("AnimationController", model)
        end
        model.Name = "__RebuildPlaybackVerification"
        for _, obj in model:GetDescendants() do
            if obj:IsA("BasePart") then obj.Anchored = true end
            if obj:IsA("BaseScript") then obj:Destroy() end
        end
        model.Parent = workspace
        local rig = Rig.new(model)
        rig:LoadAnimation(case.baked)
        sequence = rig:ToRobloxAnimation()
        assert(sequence, "No exported animation")
        sequence.Loop = true
        animation = Instance.new("Animation")
        animation.AnimationId = game:GetService("KeyframeSequenceProvider"):RegisterKeyframeSequence(sequence)
        local animator = model:FindFirstChildWhichIsA("Animator", true)
        if not animator then
            animator = Instance.new("Animator", model:FindFirstChildOfClass("Humanoid") or model:FindFirstChildOfClass("AnimationController"))
        end
        track = animator:LoadAnimation(animation)
        track:Play(0, 1, 1)
        local deadline = os.clock()+5
        repeat task.wait() until track.Length>0 or os.clock()>=deadline
        assert(math.abs(track.Length-case.baked.t)<0.0001, "Animation duration changed")
        local joints, bones = {}, {}
        for _, obj in model:GetDescendants() do
            if obj:IsA("Motor6D") then table.insert(joints,obj) end
            if obj:IsA("Bone") then table.insert(bones,obj) end
        end
        for cycle=0,2 do
            for _, sample in case.expected do
                track.TimePosition = sample.t % case.baked.t
                animator:StepAnimations(0)
                local rootName = case.metadata.rig.jname
                local world = {[rootName]=model:FindFirstChild(rootName,true).CFrame}
                local pending = table.clone(joints)
                while #pending>0 do
                    local progressed = false
                    for i=#pending,1,-1 do
                        local joint = pending[i]
                        if world[joint.Part0.Name] then
                            world[joint.Part1.Name] = world[joint.Part0.Name]*joint.C0*joint.Transform*joint.C1:Inverse()
                            table.remove(pending,i)
                            progressed=true
                        end
                    end
                    assert(progressed,"Disconnected joint graph")
                end
                for _, bone in bones do world[bone.Name]=bone.TransformedWorldCFrame end
                for name, components in sample.poses do
                    local actual=assert(world[name],"Missing exported bone "..name)
                    local difference=CFrame.new(table.unpack(components)):ToObjectSpace(actual)
                    local position=difference.Position.Magnitude
                    local _, angle=difference:ToAxisAngle()
                    angle=math.deg(math.abs(angle))
                    assert(position==position and angle==angle,"Non-finite pose")
                    report.samples+=1
                    report.maxPosition=math.max(report.maxPosition,position)
                    report.maxAngle=math.max(report.maxAngle,angle)
                    if position>0.001 or angle>0.1 then
                        report.failures+=1
                        report.firstFailure=report.firstFailure or {bone=name,time=sample.t,position=position,angle=angle}
                    end
                end
            end
        end
        report.pass=report.failures==0
        return report
    end,debug.traceback)
    if track then track:Stop(0) track:Destroy() end
    if animation then animation:Destroy() end
    if sequence then sequence:Destroy() end
    if model then model:Destroy() end
    if not ok then report.pass=false report.error=result end
    return report
end
