--!native
--!optimize 2
--!strict

-- cache frequently used functions
local string_byte = string.byte
local string_char = string.char
local string_sub = string.sub
local string_find = string.find
local string_gsub = string.gsub
local string_lower = string.lower
local string_upper = string.upper
local string_format = string.format
local string_reverse = string.reverse
local string_rep = string.rep
local string_concat = table.concat
local math_floor = math.floor
local tonumber = tonumber

type stringy = string | { [number]: string }

local function divide_string(str: string, max: number, fillChar: string?): { [number]: string }
	fillChar = fillChar or ""
	local result = {}
	local resultCount = 0

	local start = 1
	for i = 1, #str do
		if i % max == 0 then
			resultCount = resultCount + 1
			result[resultCount] = string_sub(str, start, i)
			start = i + 1
		elseif i == #str then
			resultCount = resultCount + 1
			result[resultCount] = string_sub(str, start, i)
		end
	end

	return result
end

local function number_to_bit(num: number, length: number): string
	local bits: { number } = {}

	local i = 1
	while num > 0 do
		bits[i] = num % 2
		num = num // 2
		i = i + 1
	end

	-- fill remaining slots with zeros
	for j = i, length do
		bits[j] = 0
	end

	return string_reverse(string_concat(bits))
end

local function ignore_set(str: string, set: string?): string
	if set then
		str = string_gsub(str, "[" .. set .. "]", "")
	end
	return str
end

local function pure_from_bit(str: string): string
	return (string_gsub(str, "........", function(cc)
		local num = tonumber(cc, 2)
		if not num then
			return ""
		end
		return string_char(num)
	end))
end

--------------------------------------------------------------------------------

local basexx = {}

--------------------------------------------------------------------------------
-- base2(bitfield) decode and encode function
--------------------------------------------------------------------------------

local bitMap = { o = "0", i = "1", l = "1" }

function basexx.from_bit(str: string, ignore: string?): (string?, string?)
	str = ignore_set(str, ignore)
	str = string_lower(str)
	str = string_gsub(str, "[ilo]", function(c)
		return bitMap[c]
	end)
	local wrong = str:match("[^01]")
	if wrong then
		return nil, wrong
	end

	return pure_from_bit(str)
end

function basexx.to_bit(str: string): string
	return (
		string_gsub(str, ".", function(c)
			local byte = string_byte(c)
			local bits = {}
			bits[8] = byte % 2
			bits[7] = (byte // 2) % 2
			bits[6] = (byte // 4) % 2
			bits[5] = (byte // 8) % 2
			bits[4] = (byte // 16) % 2
			bits[3] = (byte // 32) % 2
			bits[2] = (byte // 64) % 2
			bits[1] = (byte // 128) % 2
			return string_concat(bits)
		end)
	)
end

--------------------------------------------------------------------------------
-- base16(hex) decode and encode function
--------------------------------------------------------------------------------

function basexx.from_hex(str: string, ignore: string?): (string?, string?)
	str = ignore_set(str, ignore)
	local wrong = str:match("[^%x]")
	if wrong then
		return nil, wrong
	end

	return (string_gsub(str, "..", function(cc)
		local num = tonumber(cc, 16)
		if not num then
			return ""
		end
		return string_char(num)
	end))
end

function basexx.to_hex(str: string): string
	return (string_gsub(str, ".", function(c)
		return string_format("%02X", string_byte(c))
	end))
end

--------------------------------------------------------------------------------
-- generic function to decode and encode base32/base64
--------------------------------------------------------------------------------

local function from_basexx(str: string, alphabet: string, bits: number): (string?, string?)
	local result = {}
	local resultCount = 0
	
	for i = 1, #str do
		local c = string_sub(str, i, i)
		if c ~= "=" then
			local index = string_find(alphabet, c, 1, true)
			if not index then
				return nil, c
			end
			resultCount = resultCount + 1
			result[resultCount] = number_to_bit(index - 1, bits)
		end
	end

	local value = string_concat(result)
	local pad = #value % 8
	return pure_from_bit(string_sub(value, 1, #value - pad))
end

local function to_basexx(str: string, alphabet: string, bits: number, pad: string?): string
	local bitString = basexx.to_bit(str)

	local chunks = divide_string(bitString, bits)
	local result: { string } = {}
	local resultCount = 0
	
	for _, value in ipairs(chunks) do
		if #value < bits then
			value = value .. string_rep("0", bits - #value)
		end
		local num = tonumber(value, 2)
		if not num then
			continue
		end
		local pos = num + 1
		resultCount = resultCount + 1
		result[resultCount] = string_sub(alphabet, pos, pos)
	end

	if pad then
		resultCount = resultCount + 1
		result[resultCount] = pad
	end
	return string_concat(result)
end

--------------------------------------------------------------------------------
-- rfc 3548: http://www.rfc-editor.org/rfc/rfc3548.txt
--------------------------------------------------------------------------------

local base32Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
local base32PadMap = { "", "======", "====", "===", "=" }

function basexx.from_base32(str: string, ignore: string?): (string?, string?)
	str = ignore_set(str, ignore)
	return from_basexx(string_upper(str), base32Alphabet, 5)
end

function basexx.to_base32(str: string): string
	return to_basexx(str, base32Alphabet, 5, base32PadMap[#str % 5 + 1])
end

--------------------------------------------------------------------------------
-- crockford: http://www.crockford.com/wrmg/base32.html
--------------------------------------------------------------------------------

local crockfordAlphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
local crockfordMap = { O = "0", I = "1", L = "1" }

function basexx.from_crockford(str: string, ignore: string?): (string?, string?)
	str = ignore_set(str, ignore)
	str = string_upper(str)
	str = string_gsub(str, "[ILOU]", function(c)
		return crockfordMap[c]
	end)
	return from_basexx(str, crockfordAlphabet, 5)
end

function basexx.to_crockford(str: string): string
	return to_basexx(str, crockfordAlphabet, 5, "")
end

--------------------------------------------------------------------------------
-- base64 decode and encode function
--------------------------------------------------------------------------------

local base64Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" .. "abcdefghijklmnopqrstuvwxyz" .. "0123456789+/"
local base64PadMap = { "", "==", "=" }

function basexx.from_base64(str: string, ignore: string?): (string?, string?)
	str = ignore_set(str, ignore)
	return from_basexx(str, base64Alphabet, 6)
end

function basexx.to_base64(str: string): string
	return to_basexx(str, base64Alphabet, 6, base64PadMap[#str % 3 + 1])
end

--------------------------------------------------------------------------------
-- URL safe base64 decode and encode function
--------------------------------------------------------------------------------

local url64Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" .. "abcdefghijklmnopqrstuvwxyz" .. "0123456789-_"

function basexx.from_url64(str: string, ignore: string?): (string?, string?)
	str = ignore_set(str, ignore)
	return from_basexx(str, url64Alphabet, 6)
end

function basexx.to_url64(str: string): string
	return to_basexx(str, url64Alphabet, 6, "")
end

--------------------------------------------------------------------------------
--
--------------------------------------------------------------------------------

local z85Decoder = {
	0x00,
	0x44,
	0x00,
	0x54,
	0x53,
	0x52,
	0x48,
	0x00,
	0x4B,
	0x4C,
	0x46,
	0x41,
	0x00,
	0x3F,
	0x3E,
	0x45,
	0x00,
	0x01,
	0x02,
	0x03,
	0x04,
	0x05,
	0x06,
	0x07,
	0x08,
	0x09,
	0x40,
	0x00,
	0x49,
	0x42,
	0x4A,
	0x47,
	0x51,
	0x24,
	0x25,
	0x26,
	0x27,
	0x28,
	0x29,
	0x2A,
	0x2B,
	0x2C,
	0x2D,
	0x2E,
	0x2F,
	0x30,
	0x31,
	0x32,
	0x33,
	0x34,
	0x35,
	0x36,
	0x37,
	0x38,
	0x39,
	0x3A,
	0x3B,
	0x3C,
	0x3D,
	0x4D,
	0x00,
	0x4E,
	0x43,
	0x00,
	0x00,
	0x0A,
	0x0B,
	0x0C,
	0x0D,
	0x0E,
	0x0F,
	0x10,
	0x11,
	0x12,
	0x13,
	0x14,
	0x15,
	0x16,
	0x17,
	0x18,
	0x19,
	0x1A,
	0x1B,
	0x1C,
	0x1D,
	0x1E,
	0x1F,
	0x20,
	0x21,
	0x22,
	0x23,
	0x4F,
	0x00,
	0x50,
	0x00,
	0x00,
}

function basexx.from_z85(str: string, ignore: string?): (string?, string | number?)
	str = ignore_set(str, ignore)
	if (#str % 5) ~= 0 then
		return nil, #str % 5
	end

	local result = {}
	local resultCount = 0

	local value = 0
	for i = 1, #str do
		local index = string_byte(str, i) - 31
		if index < 1 or index >= #z85Decoder then
			return nil, string_sub(str, i, i)
		end
		value = (value * 85) + z85Decoder[index]
		if (i % 5) == 0 then
			local divisor = 256 * 256 * 256
			while divisor >= 1 do
				local b = (value // divisor) % 256
				resultCount = resultCount + 1
				result[resultCount] = string_char(b)
				divisor = divisor // 256
			end
			value = 0
		end
	end

	return string_concat(result)
end

local z85Encoder = "0123456789"
	.. "abcdefghijklmnopqrstuvwxyz"
	.. "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
	.. ".-:+=^!/*?&<>()[]{}@%$#"

function basexx.to_z85(str: string): (string?, number?, number?)
	if (#str % 4) ~= 0 then
		return nil, #str, 4
	end

	local result = {}
	local resultCount = 0

	local value = 0
	for i = 1, #str do
		local b = string_byte(str, i)
		value = (value * 256) + b
		if (i % 4) == 0 then
			local divisor = 85 * 85 * 85 * 85
			while divisor >= 1 do
				local index = ((value // divisor) % 85) + 1
				resultCount = resultCount + 1
				result[resultCount] = string_sub(z85Encoder, index, index)
				divisor = divisor // 85
			end
			value = 0
		end
	end

	return string_concat(result)
end

--------------------------------------------------------------------------------

return basexx
