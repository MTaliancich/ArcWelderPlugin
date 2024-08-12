This setting controls how much play Arc Welder has in converting gcode points into arcs. If the arc deviates from the
original points by + or - 1/2 of the resolution, the points will not be converted. The default setting is 0.05 which
means the arcs may not deviate by more than +- 0.025mm (which is a really tiny deviation). Increasing the resolution
will result in more arcs being converted, but will make the tool paths less accurate. Decreasing the resolution will
result in fewer arcs, but more accurate toolpaths. I don't recommend going above 0.1MM. Higher values than that may
result in print failure.