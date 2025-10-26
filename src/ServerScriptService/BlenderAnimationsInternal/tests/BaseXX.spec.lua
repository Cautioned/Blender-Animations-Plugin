--!nolint
return function()
	local testez = require(script.Parent.Parent.Components.testez)
	local basexx = require(script.Parent.Parent.Components.BaseXX)

	describe("BaseXX Module", function()
		it("should correctly perform a base32 round-trip", function()
			local originalstring = "this is a test string!"
			local encoded = basexx.to_base32(originalstring)
			local decoded = basexx.from_base32(encoded)
			expect(decoded).to.equal(originalstring)
		end)

		it("should correctly perform a base64 round-trip", function()
			local originalstring = "this is a test string!"
			local encoded = basexx.to_base64(originalstring)
			local decoded = basexx.from_base64(encoded)
			expect(decoded).to.equal(originalstring)
		end)

		it("should handle empty strings for base32", function()
			local originalstring = ""
			local encoded = basexx.to_base32(originalstring)
			local decoded = basexx.from_base32(encoded)
			expect(decoded).to.equal(originalstring)
		end)

		it("should handle empty strings for base64", function()
			local originalstring = ""
			local encoded = basexx.to_base64(originalstring)
			local decoded = basexx.from_base64(encoded)
			expect(decoded).to.equal(originalstring)
		end)
	end)
end 