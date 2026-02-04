# lumapad

Ambient lighting for your gamepads.

Note: Heavy vibe-coding 💀 but the codebase is fairly clean

## Permissions setup

Create a group called `leds` and add yourself to it:

```
groupadd leds
gpasswd -a user leds
```

In `/etc/udev/rules.d/40-leds.rules` add:

```
SUBSYSTEM=="leds", ACTION=="add", RUN+="/bin/chgrp -R leds /sys%p", RUN+="/bin/chmod -R g=u /sys%p"
SUBSYSTEM=="leds", ACTION=="change", RUN+="/bin/chgrp -R leds /sys%p", RUN+="/bin/chmod -R g=u /sys%p"
```
