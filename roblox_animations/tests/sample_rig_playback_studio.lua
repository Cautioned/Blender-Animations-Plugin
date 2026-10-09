-- Native Animator comparison against evaluated Blender Motor6D transforms.
-- Serve the generator output on localhost:18775; pass case IDs as arguments.
return function(ids)
 local http=game:GetService('HttpService')
 local old=http.HttpEnabled
 http.HttpEnabled=true
 local folder=Instance.new('Folder',game.ServerStorage)
 folder.Name='__SampleRigVerification'
 local cleanup={}
 local ok,result=xpcall(function()
  local base='http://127.0.0.1:18775/'
  for name,source in http:JSONDecode(http:GetAsync(base..'sources.json')) do
   local m=Instance.new('ModuleScript',folder) m.Name=name m.Source=source
  end
  local Rig=require(folder.Rig)
  local reports={}
  for _,id in ids do
   local case=http:JSONDecode(http:GetAsync(base..id..'.json'))
   local model=assert(workspace:FindFirstChild(case.rig),case.rig):Clone()
   table.insert(cleanup,model)
   model.Name='__SamplePlayback'
   for _,v in model:GetDescendants() do
    if v:IsA('BasePart') then v.Anchored=true elseif v:IsA('BaseScript') then v:Destroy() end
   end
   model.Parent=workspace
   local rig=Rig.new(model) rig:LoadAnimation(case.baked)
   local sequence=rig:ToRobloxAnimation() sequence.Loop=true table.insert(cleanup,sequence)
   local animation=Instance.new('Animation') table.insert(cleanup,animation)
   animation.AnimationId=game:GetService('KeyframeSequenceProvider'):RegisterKeyframeSequence(sequence)
   local animator=model:FindFirstChildWhichIsA('Animator',true) or Instance.new('Animator',model:FindFirstChildOfClass('Humanoid'))
   local track=animator:LoadAnimation(animation) table.insert(cleanup,track) track:Play(0,1,0)
   local deadline=os.clock()+5 repeat task.wait() until track.Length>0 or os.clock()>deadline
   assert(track.Length>0,'Animation did not load: '..id)
   local joints={} for _,j in model:GetDescendants() do if j:IsA('Motor6D') then joints[j.Part1.Name]=j end end
   local r={id=id,file=case.file,mode=case.mode,samples=0,failed=0,maxPosition=0,maxAngle=0,maxDrift=0,failedBones={}}
   local first={}
   for cycle=1,3 do for index,sample in case.expected do
    -- Keep this offset below the before/after-discontinuity probes.
    track.TimePosition=sample.t+0.0000002 animator:StepAnimations(0) first[index]=first[index] or {}
    for name,cf in sample.poses do
     local joint=assert(joints[name],'Missing joint '..name)
     local actual=joint.Transform
     local expected=CFrame.new(table.unpack(cf))
     -- Serialized matrices are rounded; normalize the reference basis before
     -- measuring angle so tiny scale errors are not reported as rotation.
     local x=expected.XVector.Unit local z=x:Cross(expected.YVector).Unit
     expected=CFrame.fromMatrix(expected.Position,x,z:Cross(x),z)
     local delta=expected:ToObjectSpace(actual)
     local p=delta.Position.Magnitude local _,angle=delta:ToAxisAngle() angle=math.deg(math.abs(angle))
     r.samples+=1 r.maxPosition=math.max(r.maxPosition,p) r.maxAngle=math.max(r.maxAngle,angle)
     if p>0.002 or angle>0.15 or p~=p or angle~=angle then
      r.failed+=1 r.failedBones[name]=(r.failedBones[name] or 0)+1
      r.firstFailure=r.firstFailure or {time=sample.t,bone=name,position=p,angle=angle}
     end
     if cycle==1 then first[index][name]=actual else r.maxDrift=math.max(r.maxDrift,(actual.Position-first[index][name].Position).Magnitude) end
    end
   end end
   r.pass=r.failed==0 and r.samples>0 and r.maxDrift<0.001
   table.insert(reports,r)
   track:Stop(0)
   for i=#cleanup,1,-1 do cleanup[i]:Destroy() end table.clear(cleanup)
  end
  return reports
 end,debug.traceback)
 for i=#cleanup,1,-1 do cleanup[i]:Destroy() end folder:Destroy() http.HttpEnabled=old
 assert(ok,result) return result
end
