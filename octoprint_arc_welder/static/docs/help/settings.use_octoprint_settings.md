Octoprint has a setting for *G90/G91 influences extruder* in the *Features* tab. Enabling *Use Octoprint Printer
Settings* will cause **Arc Welder** to use OctoPrint's setting. If this is unchecked, you can specify the *G90/G91
influences extruder* manually. Default: Enabled

Note:  This is an EXTREMELY important setting. In general, Marlin 2.0 and forks should have this box checked. Many forks
of Marlin 1.x should have this unchecked, like the Prusa MK2 and MK3. If this setting is incorrect, inaccurate GCode may
be produced.