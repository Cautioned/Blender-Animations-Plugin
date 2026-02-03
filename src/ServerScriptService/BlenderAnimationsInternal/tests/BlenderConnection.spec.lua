return function()
	local testez = require(script.Parent.Parent.Components.testez)
	local BlenderConnection = require(script.Parent.Parent.Components.BlenderConnection)

	describe("BlenderConnection", function()
		local connection
		local mockHttpService

		beforeEach(function()
			-- Create a mock HttpService for each test to ensure isolation
			mockHttpService = {
				_getAsyncResponse = nil,
				_getAsyncShouldSucceed = true,
				_requestAsyncResponse = nil,
				_requestAsyncShouldSucceed = true,
				_jsonDecodeResponse = {},
				_jsonDecodeShouldSucceed = true,

				GetAsync = function(self, _)
					if self._getAsyncShouldSucceed then
						return self._getAsyncResponse
					else
						error("Mock HttpService: GetAsync failed")
					end
				end,

				RequestAsync = function(self, _)
					if self._requestAsyncShouldSucceed then
						return self._requestAsyncResponse
					else
						error("Mock HttpService: RequestAsync failed")
					end
				end,

				JSONDecode = function(self, _)
					if self._jsonDecodeShouldSucceed then
						return self._jsonDecodeResponse
					else
						error("Mock HttpService: JSONDecode failed")
					end
				end,

				JSONEncode = function(self, data)
					-- For export, we just need a passthrough
					return "encoded_json"
				end,
			}
			connection = BlenderConnection.new(mockHttpService)
		end)

		describe("ListArmatures", function()
			it("should return armature data on success", function()
				local armatures = { { name = "Armature1" }, { name = "Armature2" } }
				-- ListArmatures uses RequestAsync, not GetAsync
				mockHttpService._requestAsyncResponse = {
					Success = true,
					Body = '{"armatures": [{"name": "Armature1"}, {"name": "Armature2"}]}'
				}
				mockHttpService._jsonDecodeResponse = { armatures = armatures }

				local result = connection:ListArmatures(1337)
				expect(result).to.be.ok()
				expect(#result).to.equal(2)
				expect(result[1].name).to.equal("Armature1")
			end)

			it("should return nil if RequestAsync fails", function()
				mockHttpService._requestAsyncShouldSucceed = false
				local result = connection:ListArmatures(1337)
				expect(result).to.equal(nil)
			end)

			it("should return nil if JSONDecode fails", function()
				mockHttpService._requestAsyncResponse = {
					Success = true,
					Body = "invalid json"
				}
				mockHttpService._jsonDecodeShouldSucceed = false
				local result = connection:ListArmatures(1337)
				expect(result).to.equal(nil)
			end)
		end)

		describe("ImportAnimation", function()
			it("should return response body on success", function()
				mockHttpService._requestAsyncResponse = {
					Success = true,
					Body = "animation_data_body",
				}
				local result = connection:ImportAnimation(1337, "TestArmature")
				expect(result).to.equal("animation_data_body")
			end)

			it("should return nil if RequestAsync pcall fails", function()
				mockHttpService._requestAsyncShouldSucceed = false
				local result = connection:ImportAnimation(1337, "TestArmature")
				expect(result).to.equal(nil)
			end)

			it("should return nil if response.Success is false", function()
				mockHttpService._requestAsyncResponse = {
					Success = false,
					StatusMessage = "Not Found",
				}
				local result = connection:ImportAnimation(1337, "TestArmature")
				expect(result).to.equal(nil)
			end)
		end)

		describe("ExportAnimation", function()
			it("should return true on success", function()
				mockHttpService._requestAsyncResponse = {
					Success = true,
				}
				local result = connection:ExportAnimation(1337, { data = "test" })
				expect(result).to.equal(true)
			end)

			it("should return false if RequestAsync pcall fails", function()
				mockHttpService._requestAsyncShouldSucceed = false
				local result = connection:ExportAnimation(1337, { data = "test" })
				expect(result).to.equal(false)
			end)

			it("should return false if response.Success is false", function()
				mockHttpService._requestAsyncResponse = {
					Success = false,
					StatusMessage = "Server Error",
				}
				local result = connection:ExportAnimation(1337, { data = "test" })
				expect(result).to.equal(false)
			end)
		end)
	end)
end 