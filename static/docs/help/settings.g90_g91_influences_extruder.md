If *Use Octoprint Printer Settings* is unchecked, **Arc Welder** will use this setting to determine if the G90/G91
command influences your extruder's axis mode. In general, Marlin 2.0 and forks should have this box checked. Many forks
of Marlin 1.x should have this unchecked, like the Prusa MK2 and MK3. I will try to add a list of printers and the
proper value for this setting at some point, as well as a gcode test script you can use to determine what setting to
use. Keep in mind that most slicers produce code that will work fine no matter what setting you choose here. Default:
Disabled