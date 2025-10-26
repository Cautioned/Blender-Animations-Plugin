return function(props, exclude)
    local export = table.clone(props)
    for _, k in ipairs(exclude) do
        export[k] = nil
    end
    return export
end
