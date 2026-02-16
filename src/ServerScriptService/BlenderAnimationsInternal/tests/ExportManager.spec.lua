return function()
	local ExportManager = require(script.Parent.Parent.Services.ExportManager)
	local State = require(script.Parent.Parent.state)

	describe("ExportManager", function()
		local exportManager
		local mockRig

		beforeEach(function()
			exportManager = ExportManager.new()

			-- Build a minimal rig model
			mockRig = Instance.new("Model")
			mockRig.Name = "MockRig"
			mockRig.Parent = workspace

			local hrp = Instance.new("Part")
			hrp.Name = "HumanoidRootPart"
			hrp.Parent = mockRig
			mockRig.PrimaryPart = hrp

			local rightHand = Instance.new("Part")
			rightHand.Name = "RightHand"
			rightHand.Parent = mockRig

			local handMotor = Instance.new("Motor6D")
			handMotor.Name = "RightWrist"
			handMotor.Part0 = hrp
			handMotor.Part1 = rightHand
			handMotor.Parent = hrp

			State.activeRigModel = mockRig
		end)

		afterEach(function()
			if mockRig then
				mockRig:Destroy()
			end
			State.activeRigModel = nil
			State.lastKnownRigModel = nil
		end)

		describe("resolveWeapon", function()
			it("should resolve a Tool as container", function()
				local tool = Instance.new("Tool")
				tool.Name = "Sword"
				local handle = Instance.new("Part")
				handle.Name = "Handle"
				handle.Parent = tool
				tool.Parent = workspace

				local container, root = exportManager:resolveWeapon(tool)

				expect(container).to.equal(tool)
				expect(root).to.equal(handle)

				tool:Destroy()
			end)

			it("should resolve a Model as container", function()
				local model = Instance.new("Model")
				model.Name = "WeaponModel"
				local blade = Instance.new("Part")
				blade.Name = "Blade"
				blade.Parent = model
				model.PrimaryPart = blade
				model.Parent = workspace

				local container, root = exportManager:resolveWeapon(model)

				expect(container).to.equal(model)
				expect(root).to.equal(blade)

				model:Destroy()
			end)

			it("should resolve a bare Part directly without container", function()
				local part = Instance.new("Part")
				part.Name = "LooseSword"
				part.Parent = workspace

				local container, root = exportManager:resolveWeapon(part)

				expect(container).to.never.be.ok()
				expect(root).to.equal(part)

				part:Destroy()
			end)

			it("should resolve a bare MeshPart directly without container", function()
				local mesh = Instance.new("MeshPart")
				mesh.Name = "LooseMesh"
				mesh.Parent = workspace

				local container, root = exportManager:resolveWeapon(mesh)

				expect(container).to.never.be.ok()
				expect(root).to.equal(mesh)

				mesh:Destroy()
			end)

			it("should NOT use the active rig as container for a part inside the rig", function()
				-- Simulate selecting a part that is a child of the active rig
				local rigPart = Instance.new("Part")
				rigPart.Name = "WeaponPart"
				rigPart.Parent = mockRig

				local container, root = exportManager:resolveWeapon(rigPart)

				-- Should NOT resolve to the rig model as container
				expect(container).to.never.equal(mockRig)
				-- Should treat it as a bare part
				expect(root).to.equal(rigPart)

				rigPart:Destroy()
			end)

			it("should find a Tool container for a part inside a Tool", function()
				local tool = Instance.new("Tool")
				tool.Name = "TestTool"
				tool.Parent = workspace

				local handle = Instance.new("Part")
				handle.Name = "Handle"
				handle.Parent = tool

				local blade = Instance.new("Part")
				blade.Name = "Blade"
				blade.Parent = tool

				-- Selecting the blade should resolve to the tool container
				local container, root = exportManager:resolveWeapon(blade)

				expect(container).to.equal(tool)
				-- root should be handle (it's the primary by convention) or blade
				expect(root).to.be.ok()

				tool:Destroy()
			end)

			it("should resolve an Accessory as container", function()
				local acc = Instance.new("Accessory")
				acc.Name = "HatWeapon"
				local handle = Instance.new("Part")
				handle.Name = "Handle"
				handle.Parent = acc
				acc.Parent = workspace

				local container, root = exportManager:resolveWeapon(acc)

				expect(container).to.equal(acc)
				expect(root).to.equal(handle)

				acc:Destroy()
			end)

			it("should find a Tool equipped on the rig without rejecting it", function()
				-- Tool is parented under the rig, but it's a Tool so the rig exclusion
				-- should NOT block it — we only block the rig Model itself as container
				local tool = Instance.new("Tool")
				tool.Name = "EquippedSword"
				local handle = Instance.new("Part")
				handle.Name = "Handle"
				handle.Parent = tool
				tool.Parent = mockRig

				local container, root = exportManager:resolveWeapon(handle)

				expect(container).to.equal(tool)
				expect(root).to.equal(handle)

				tool:Destroy()
			end)

			it("should return nil root for a Model with no BaseParts", function()
				local model = Instance.new("Model")
				model.Name = "EmptyModel"
				model.Parent = workspace

				local container, root = exportManager:resolveWeapon(model)

				expect(container).to.equal(model)
				expect(root).to.never.be.ok()

				model:Destroy()
			end)

			it("should auto-detect the most-connected hub as root", function()
				local model = Instance.new("Model")
				model.Name = "MultiPartWeapon"
				model.Parent = workspace

				local hub = Instance.new("Part")
				hub.Name = "Hub"
				hub.Parent = model

				local blade1 = Instance.new("Part")
				blade1.Name = "Blade1"
				blade1.Parent = model

				local blade2 = Instance.new("Part")
				blade2.Name = "Blade2"
				blade2.Parent = model

				local weld1 = Instance.new("Motor6D")
				weld1.Part0 = hub
				weld1.Part1 = blade1
				weld1.Parent = hub

				local weld2 = Instance.new("Motor6D")
				weld2.Part0 = hub
				weld2.Part1 = blade2
				weld2.Parent = hub

				local container, root = exportManager:resolveWeapon(model)

				expect(container).to.equal(model)
				expect(root).to.equal(hub)

				model:Destroy()
			end)

			it("should prefer rig-connected part over PrimaryPart", function()
				local model = Instance.new("Model")
				model.Name = "M4A1"
				model.Parent = workspace

				local main = Instance.new("Part")
				main.Name = "Main"
				main.Parent = model
				model.PrimaryPart = main

				local handle = Instance.new("Part")
				handle.Name = "Handle"
				handle.Parent = model

				-- Motor6D from rig's RightHand to weapon's Handle
				local rightHand = mockRig:FindFirstChild("RightHand")
				local grip = Instance.new("Motor6D")
				grip.Name = "RightGrip"
				grip.Part0 = rightHand
				grip.Part1 = handle
				grip.Parent = rightHand

				local container, root = exportManager:resolveWeapon(model)

				expect(container).to.equal(model)
				-- should pick Handle (rig-connected) not Main (PrimaryPart)
				expect(root).to.equal(handle)

				grip:Destroy()
				model:Destroy()
			end)
		end)

		describe("detectWeaponConnection", function()
			it("should detect Motor6D connection between weapon and rig", function()
				local weapon = Instance.new("Part")
				weapon.Name = "Handle"
				weapon.Parent = workspace

				-- Create a Motor6D connecting the rig's RightHand to the weapon
				local rightHand = mockRig:FindFirstChild("RightHand")
				local grip = Instance.new("Motor6D")
				grip.Name = "RightGrip"
				grip.Part0 = rightHand
				grip.Part1 = weapon
				grip.Parent = rightHand

				State.selectedWeapon:set(weapon)
				exportManager:detectWeaponConnection()

				local status = State.weaponConnectionStatus:get()
				-- Should contain the checkmark
				expect(status:find(utf8.char(0x2713))).to.be.ok()
				expect(status:find("RightHand")).to.be.ok()

				grip:Destroy()
				weapon:Destroy()
				State.selectedWeapon:set(nil)
				State.weaponConnectionStatus:set("")
			end)

			it("should detect Weld connection between weapon and rig", function()
				local weapon = Instance.new("Part")
				weapon.Name = "Handle"
				weapon.Parent = workspace

				local rightHand = mockRig:FindFirstChild("RightHand")
				local weld = Instance.new("Weld")
				weld.Name = "GripWeld"
				weld.Part0 = rightHand
				weld.Part1 = weapon
				weld.Parent = rightHand

				State.selectedWeapon:set(weapon)
				exportManager:detectWeaponConnection()

				local status = State.weaponConnectionStatus:get()
				expect(status:find(utf8.char(0x2713))).to.be.ok()

				weld:Destroy()
				weapon:Destroy()
				State.selectedWeapon:set(nil)
				State.weaponConnectionStatus:set("")
			end)

			it("should detect WeldConstraint connection between weapon and rig", function()
				local weapon = Instance.new("Part")
				weapon.Name = "Handle"
				weapon.Parent = workspace

				local rightHand = mockRig:FindFirstChild("RightHand")
				local wc = Instance.new("WeldConstraint")
				wc.Part0 = rightHand
				wc.Part1 = weapon
				wc.Parent = rightHand

				State.selectedWeapon:set(weapon)
				exportManager:detectWeaponConnection()

				local status = State.weaponConnectionStatus:get()
				expect(status:find(utf8.char(0x2713))).to.be.ok()

				wc:Destroy()
				weapon:Destroy()
				State.selectedWeapon:set(nil)
				State.weaponConnectionStatus:set("")
			end)

			it("should detect connection to one part of a multi-part weapon Model", function()
				local model = Instance.new("Model")
				model.Name = "WeaponModel"
				model.Parent = workspace

				local handle = Instance.new("Part")
				handle.Name = "Handle"
				handle.Parent = model
				model.PrimaryPart = handle

				local blade = Instance.new("Part")
				blade.Name = "Blade"
				blade.Parent = model

				local rightHand = mockRig:FindFirstChild("RightHand")
				local grip = Instance.new("Motor6D")
				grip.Name = "RightGrip"
				grip.Part0 = rightHand
				grip.Part1 = handle
				grip.Parent = rightHand

				State.selectedWeapon:set(model)
				exportManager:detectWeaponConnection()

				local status = State.weaponConnectionStatus:get()
				expect(status:find(utf8.char(0x2713))).to.be.ok()
				expect(status:find("Handle")).to.be.ok()

				grip:Destroy()
				model:Destroy()
				State.selectedWeapon:set(nil)
				State.weaponConnectionStatus:set("")
			end)

			it("should report no connection when weapon is unattached", function()
				local weapon = Instance.new("Part")
				weapon.Name = "FloatingWeapon"
				weapon.Parent = workspace

				State.selectedWeapon:set(weapon)
				exportManager:detectWeaponConnection()

				local status = State.weaponConnectionStatus:get()
				-- Should contain the warning symbol
				expect(status:find(utf8.char(0x26A0))).to.be.ok()

				weapon:Destroy()
				State.selectedWeapon:set(nil)
				State.weaponConnectionStatus:set("")
			end)

			it("should report no rig when none is selected", function()
				State.activeRigModel = nil
				State.lastKnownRigModel = nil

				local weapon = Instance.new("Part")
				weapon.Name = "Handle"
				weapon.Parent = workspace

				State.selectedWeapon:set(weapon)
				exportManager:detectWeaponConnection()

				local status = State.weaponConnectionStatus:get()
				expect(status:find("No rig")).to.be.ok()

				weapon:Destroy()
				State.selectedWeapon:set(nil)
				State.weaponConnectionStatus:set("")
			end)
		end)
	end)
end
