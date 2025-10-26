return function()
	local testez = require(script.Parent.Parent.Components.testez)
	local deflatelua = require(script.Parent.Parent.Components.DeflateLua)

	describe("DeflateLua Module", function()
		it("should correctly decompress a zlib stream", function()
			-- This is "hello world" compressed with zlib
			local compressed_data = "\x78\x9c\xcb\x48\xcd\xc9\xc9\x57\x28\xcf\x2f\xca\x49\x01\x00\x1a\x0b\x04\x5d"
			local expected_decompressed = "hello world"
			
			local output_buffer = {}
			local function collect_byte(byte)
				table.insert(output_buffer, string.char(byte))
			end

			deflatelua.inflate_zlib({
				input = compressed_data,
				output = collect_byte
			})

			local decompressed_data = table.concat(output_buffer)
			expect(decompressed_data).to.equal(expected_decompressed)
		end)
	end)
end 