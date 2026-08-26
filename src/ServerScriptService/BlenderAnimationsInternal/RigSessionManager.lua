--!native
--!strict

-- Owns the lifecycle of rig sessions.  State remains a compatibility view of
-- the active session while older services are incrementally moved off globals.

local RigSession = require(script.Parent.RigSession)

local RigSessionManager = {}
RigSessionManager.__index = RigSessionManager

export type Manager = {
	state: any,
	sessions: { [Instance]: RigSession.Snapshot },
	activate: (self: Manager, rig: Instance) -> boolean,
	createAndActivate: (self: Manager, rig: Instance) -> boolean,
	remove: (self: Manager, rig: Instance) -> boolean,
	contains: (self: Manager, rig: Instance) -> boolean,
	saveActive: (self: Manager) -> (),
	clear: (self: Manager) -> (),
}

function RigSessionManager.new(state: any): Manager
	local self = setmetatable({
		state = state,
		sessions = {} :: { [Instance]: RigSession.Snapshot },
	}, RigSessionManager)
	-- Keep this alias during the transition so playback and export keep working.
	state.rigSessions = self.sessions
	return self :: any
end

function RigSessionManager:contains(rig: Instance): boolean
	return self.sessions[rig] ~= nil
end

function RigSessionManager:saveActive()
	local active = self.state.activeSessionRig:get()
	if active then
		self.sessions[active] = RigSession.createSnapshot(self.state)
		self.state.rigSessionRevision:set(self.state.rigSessionRevision:get() + 1)
	end
end

function RigSessionManager:activate(rig: Instance): boolean
	if self.state.activeSessionRig:get() == rig then
		return true
	end

	local session = self.sessions[rig]
	if not session then
		return false
	end

	self:saveActive()
	RigSession.restoreSnapshot(self.state, session)
	self.state.activeSessionRig:set(rig)
	return true
end

function RigSessionManager:createAndActivate(rig: Instance): boolean
	if self:contains(rig) then
		return self:activate(rig)
	end

	self:saveActive()
	self.state.activeSessionRig:set(rig)
	return true
end

function RigSessionManager:remove(rig: Instance): boolean
	if not self.sessions[rig] and self.state.activeSessionRig:get() ~= rig then
		return false
	end
	self.sessions[rig] = nil
	return true
end

function RigSessionManager:clear()
	table.clear(self.sessions)
	self.state.activeSessionRig:set(nil)
end

return RigSessionManager
