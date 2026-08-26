return function()
	local AnimationSimplifier = require(script.Parent.Parent.Components.AnimationSimplifier)

	describe("AnimationSimplifier", function()
		describe("roundPrecision", function()
			it("should round wrapped CFrame component tables without error", function()
				local keyframes = {
					{ t = 0, kf = { Head = { 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1 } } },
					{
						t = 0.5,
						kf = {
							Head = {
								components = { 0.123456, 1.234567, 2.345678, 1, 0, 0, 0, 1, 0, 0, 0, 1 },
								easingStyle = "Linear",
								easingDirection = "Out",
							},
							Jaw = {
								{ 3.456789, 4.567891, 5.678912, 1, 0, 0, 0, 1, 0, 0, 0, 1 },
								"Linear",
								"Out",
							},
						},
						fc = {
							JawDrop = { value = 0.123456 },
							EyesLookDown = { 0.987654, "Linear", "Out" },
							BrowLowerer = 0.555555,
						},
					},
					{ t = 1, kf = { Head = { 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1 } } },
				}

				AnimationSimplifier.roundPrecision(keyframes, 3)

				expect(keyframes[2].kf.Head.components[1]).to.be.near(0.123, 0.0001)
				expect(keyframes[2].kf.Jaw[1][1]).to.be.near(3.457, 0.0001)
				expect(keyframes[2].fc.JawDrop.value).to.be.near(0.123, 0.0001)
				expect(keyframes[2].fc.EyesLookDown[1]).to.be.near(0.988, 0.0001)
				expect(keyframes[2].fc.BrowLowerer).to.be.near(0.556, 0.0001)
			end)
		end)
	end)
end
