return function(x: any, useDependency: boolean?): any
	if typeof(x)=="table" and x.type=="State" then
		return x:get(useDependency)
	end
	return x
end