This setting can be used as a workaround for firmware that tends to draw arc with a very small radius as a flat edge or
an obvious polygon. It controls how many segments should appear for a full circle of the same radius as the arc. For
older versions and forks of Marlin (including all current Prusa firmware), I recommend a value of 14 here.

This will only work if *MM Per Arc Segment* is also greater than zero.  **Only set this if you know you need it!**.
Default: 14