Indigo-LightTools

A simple plugin for use with Indigo Domotics

I've never been good at programming. I can write a script.. then come back to it a month later and be like "I did not write this". However for ages I've been wanting to write some plugins that I thought would be useful for Indigo and the community, so with a lot of help from various AI bots I've been able to create a few plugins that seem to be working pretty much exactly as I wanted them to. Here's my first attempt, LightTools. It's probably about 80% AI code, but I've done all the debugging and testing. I can't say I'm a particularly big fan of AI for most things, but it certainly has come in helpful here.

I’d like to say thanks to everyone who offered help with this, particularly DaveL17!

This Plugin does 4 basic things:

1 - Variable Linked Dimmer - This is super simplistic and designed with one single purpose, to be able to publish a dimmer to HomeKit which can control the brightness of wall mounted iPads in my house that are using GlennNZs amazing Kiosk Home app. Simply setup a dimmer using the plugin, Link it to the brightness level variable setup for Kiosk Home, set the scale (in this situation 0 to 1). If you set the dimmer to say 70% then it'll set the variable value to 0.7 and the iPad linked to it will change its brightness accordingly. If you manually change the variable value the plugin will automatically adjust the dimmer value accordingly. It'll also attempt to validate the value entered, so if you enter invalid characters or values that fall outside the specified scale range then it'll automatically replace them with what it thinks is the most valid option.

2 - A flashing action - Again very simple, select the action and you get an option for the light you want to flash, how many times you want to flash it, maximum and minimum levels, and there's also an option to cancel flashing in progress. I've been using this to alert me when the wife arrives home. It works quite well, but occasionally the flashing will be a bit weird if the queue to send the flashes to your devices main interface becomes saturated so don't use it too excessively.

3 - Scene Controller - A device where you select devices and/or variables, save the current state of them, and then if the current state of said devices and variables match them then it'll show as on, if not it'll show as off. If you turn it on it'll set those devices to their saved values/variables. I've been using it with Scenes in HomeKit and it seems to work quite well, although occasionally it'll not show correctly - If anyone has any ideas on how to fix that please let me know.

4 - Relay to Dimmer (and Relay to Fan) - Allows you to use 2 relays to create a 3 state dimmer. The concept is if you have just relay 1 on it'll set the brightness of the dimmer to 33%. If you have just relay 2 on it'll go to 66%. And If you have both relays 1 and 2 on it'll set to 100%. I've been using this with a 3 speed fan I have that I modified.

If you find any issues or have any suggestions I'd very much appreciate the feedback on them. I've also managed to put together a fair few other plugins that I think that will be useful and I'll hopefully be releasing them over the coming weeks.

Use is at your own risk.
