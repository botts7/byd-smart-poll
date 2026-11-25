# Setup guide

Everything except the car is optional: each signal you add lets BYD Smart Poll see something
without waking the car, and each one you skip falls back to the car's own data at its next
poll.

Add the integration under Settings, Devices and services, Add integration, BYD Smart Poll.
Change anything later under Configure on the same page.

To see what is actually being used, look at the `signals_in_use` attribute of the
**Polling mode** sensor, for example `["charger power", "cable connected", "phone bluetooth (1)",
"odometer check", "car battery power"]`. A signal is only listed while its entity has a usable
state right now.

Contents: [Requirements](#requirements), [Install and remove](#install-and-remove),
[Options](#options), [Drive detection](#drive-detection), [Android driver](#android-driver),
[iPhone driver](#iphone-driver), [Chargers](#chargers), [Charging](#charging),
[Two drivers, two cars](#two-drivers-two-cars), [Diagnostics](#diagnostics),
[Repairs](#repairs).

## Requirements

- **Home Assistant 2025.2 or newer.** The Marketplace and HACS do not offer it to older
  versions.
- **BYD Vehicle 0.0.88 or newer** (a beta of 0.0.88 is fine). Older versions had no per-car
  poll interval numbers. With an older one, setup stops with "BYD Smart Poll needs BYD Vehicle
  0.0.88 or newer, and X is installed", and an existing entry shows "Needs BYD Vehicle 0.0.88 or
  newer, and X is installed" on its card. Nothing is changed on the car. Update BYD Vehicle,
  restart Home Assistant, and it sets itself up.
- **BYD Vehicle 0.0.93 or newer** is best. It applies a new poll interval at once. On 0.0.88
  to 0.0.92 its timer only picks up a new rate after its next poll, so Smart Poll sends one
  poll when it speeds the rate up; a slower rate starts after one more poll at the old rate.

## Install and remove

**Marketplace (Home Assistant 2026.11 or newer).** The Marketplace is HACS built into Home
Assistant. BYD Smart Poll is not in its catalog yet, so add it from its link:

[![Open BYD Smart Poll in the Marketplace of your Home Assistant.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=botts7&repository=byd-smart-poll&category=integration)

1. Or Settings, Marketplace, **Add from link**, and enter `botts7/byd-smart-poll`. The first
   time, the Marketplace asks you to accept its warning and to connect GitHub (it only reads
   public information).
2. Open BYD Smart Poll in the Marketplace and select **Install**. It loads straight away, no
   restart needed for the first install. Updates show under Settings, Updates; restart after
   each one (a repair offers it).

**HACS (Home Assistant 2025.2 to 2026.10).** HACS, the three dots menu, Custom repositories.
Add `https://github.com/botts7/byd-smart-poll` with type Integration. Find BYD Smart Poll, download it, and restart
Home Assistant. When you
later update to 2026.11, the Marketplace takes it over with its updates.

**Manual.** Copy `custom_components/byd_smart_poll` into the `custom_components` folder of your
Home Assistant config, then restart. Manual copies do not get updates.

Then set it up: [![Start setting up BYD Smart Poll in your Home Assistant.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=byd_smart_poll)

**Remove.** Settings, Devices and services, BYD Smart Poll, Delete. The BYD poll intervals go
back to what they were before Smart Poll was set up, or to BYD's default 300 s if that is not
known. If BYD Vehicle or the car is gone when you delete it, a warning in the log says which
values to set by hand. Turning off the **Smart polling** switch or disabling the entry also
puts the intervals back, once the BYD numbers are available.

The car is found by its VIN, so removing BYD Vehicle and adding it again (same account) does
not break Smart Poll.

## Options

Configure walks through the charging, drivers and car Bluetooth pages from setup, then
**Poll intervals, timing and battery**. Interval fields are in seconds, 30 s to 8 h (the range
BYD Vehicle accepts).

| Field | Default | When to change it |
|---|---|---|
| Parked away: telemetry | 3600 s (1 h) | Away from home, or where the car is cannot be told. Each poll wakes the car. Longer saves battery, but a charge or a drive nothing reported is seen later. |
| Parked at home: telemetry | 3600 s (1 h) | Longer (2 to 4 h) once charger power, cable and a phone signal for every driver all work. See [Diagnostics](#diagnostics). |
| GPS when not driving | 28800 s (8 h) | Rarely. A fix is taken at the end of every drive anyway. |
| Driving: telemetry | 60 s | Also the rate in the wake window. Longer means a drive start is seen later. |
| Driving: GPS | 120 s | Longer if you do not need a live position. |
| Charging: longest gap | 1800 s (30 min) | Only a backstop: charge polls are timed per percent. |
| Battery size (kWh) | empty | Optional, 5 to 250 kWh (plug-in hybrid packs are fine). The usable size gives a first guess of energy per 1%: size / 100 plus about 10 % for losses (82.5 kWh gives 0.917). |
| Energy per 1%, starting guess | empty | Optional, 0.15 to 3 kWh. Overrides the guess from the battery size. With neither set, 0.6 kWh. Only set it if you know your car's figure. |
| Energy per 1%: upper band from | 80 % | 50 to 95 %. Energy per 1% is learned separately from this level up, since most cars take more per percent near full. |
| Charging threshold | 300 W | 50 to 2000 W. Power that counts as charging, from the charger and from the car's own reading. Raise it if your charger shows standby power; keep it well below your slowest charge (a wall socket). |
| Charging stop delay | 120 s | 0 to 3600 s. How long power must stay under the threshold before charging counts as stopped, so a cloud over solar does not end it. Unplugging ends it at once. |
| Wake window | 10 min | 1 to 60 min. How long to poll at the driving rate after a phone connects or a wake hint. Raise it if you often sit in the car for a while before driving off. |
| I do not charge at home (charging page) | off | Tick it if the car is only charged away from home. Repairs then stops asking for a charger sensor. It cannot be ticked together with a charger sensor. |

**What restarts the learning.** Energy per 1% is learned while charging and kept across
restarts. Only typing a new value in "Energy per 1%, starting guess" starts both bands again
from it. Entering or changing the battery size after charging has learned something keeps
what was learned (before that, it is the starting guess). Moving the band edge keeps what
was learned. Clearing the energy per 1% field falls back to the battery size for the next
starting guess, and keeps what was learned.

## Drive detection

BYD never pushes "car switched on". A drive is only seen when a poll finds the car on. So
something has to say a drive may be about to start, and then the car is polled every minute
for a while. That is the **wake window** (mode "Expecting a drive").

**What starts it:** an Android phone connecting to this car's Bluetooth, or a wake hint
turning on. Wake hints:

- toggles (`input_boolean`, `switch`) and binary sensors count when they turn on;
- buttons and `input_button`s count on each press, the very first one too;
- anything else (a sensor) counts when it changes to on, connected or true (any case), not on
  every change. A device tracker or person going "home" does not count.

Phones and hints are ignored while the charger cable is in: the car cannot be driven, and
phones reconnect to a parked car. Plugging in ends a running window.

**When it ends early.** A false wake (a phone that briefly reconnects to the parked car, a
hint from another car) should not poll every minute for the whole window. So two polls in a
row that find the car off end it and go back to parked rates. They must be at least 30 s apart
and from 3 minutes in (a third of the way in for a window over 10 minutes). At the default
settings a false wake costs about 4 minutes of polling instead of 10.

**When it does not end early:**

- while a phone is still connected to this car, or the wake hint that started the window is
  still on. Phones connect at unlock, often minutes before the car is switched on. Once the
  phone has gone or the hint is off, the next poll that finds the car off ends it;
- when the window started from unplugging with a phone in the car (or a hint on). That window
  runs its full length, also if another hint (Android Auto finishing its connection) comes
  during it.

A car switched on resets the count, and a new phone or hint event starts a new window. If the
car only reports on after the window is over, the drive is seen at the next parked poll or
from the odometer. If that happens often, raise the wake window.

**Unplugging** takes one reading (the car is usually still awake from charging), but does not
count as a drive on its own, since the cable is not always pulled to drive.

**Just parked.** When a drive ends, one more GPS fix is taken while the car is awake, so it
shows where it was parked. Two more readings follow about 3 and 8 minutes after parking, so a
charge started right after a drive (plugged in at home, or a fast charger) is seen within
minutes. They are skipped once the car is driving or charging again, or when it is at home or
plugged in with a working charger power sensor.

**Missed drives.** At every parked poll the odometer is compared with the last one. If it
moved, GPS is fetched at once and the old fix no longer counts for "at home". The Polling mode
sensor's `last_missed_drive` attribute shows when this last happened.

## Android driver

1. In the Home Assistant app on the phone: Companion app settings, Manage sensors, Bluetooth
   connection. Turn it on.
2. Make sure the phone has connected to the car at least once, so the car is in its paired
   devices.
3. In setup, at "Who drives it?", pick the phone's `sensor.YOUR_PHONE_bluetooth_connection`.
4. The next step lists the phone's paired devices. Pick the car (it is usually preselected if
   its name has BYD in it). It is saved by MAC address. A name typed by hand must be the whole
   name, in any case: "BYD SEAL" does not match "BYD SEALION 7".

Android Auto can be added as a wake hint too. It also turns on in other cars, so Bluetooth is
the better signal if a driver uses more than one car.

## iPhone driver

The iOS companion app has no sensor for connected Bluetooth devices. Use a Shortcuts personal
automation instead. It works with any car's Bluetooth; CarPlay is not needed.

1. In Home Assistant, Settings, Devices and services, Helpers, create a **Button**
   (`input_button`), for example `input_button.car_connected`.
2. On the iPhone, open Shortcuts, Automation, New automation, **Bluetooth**, pick the car's
   Bluetooth device, "Is Connected". Choose **Run Immediately**.
3. Add the Home Assistant app's **Perform action** action (called Call service in older app
   versions): `input_button.press` on `input_button.car_connected`.
4. In Smart Poll, at "Who drives it?", pick `input_button.car_connected` as a wake hint.

Each press starts the wake window. A button never holds the window open, so it can end early
from 3 minutes in if the car is still off.

Or use a **Toggle** (`input_boolean`) instead: one automation on "Is Connected" turns it on,
a second on "Is Disconnected" turns it off. While it is on, the window does not end early
(like an Android phone still connected). It must go off again for the next drive to count.

The iPhone has to reach Home Assistant from wherever the car is parked, so Home Assistant
needs remote access (Home Assistant Cloud or your own external URL).

The setup page describes the same thing with a CarPlay trigger. That works too if the car has
CarPlay, but the Bluetooth trigger also covers cars and drivers without it.

## Chargers

Use one charger power sensor per car, showing only this car's charging. Power in W, kW or any
power unit works; a sensor with no unit is read as W. Energy (kWh) or current (A) sensors are
refused. BYD Vehicle's own sensors are not offered.

The cable sensor is a `binary_sensor` that is on while the cable is plugged into the car. It
means "at home", and unplugging takes a reading. It must mean plugged into the car, not the
cable hanging in its holder. Skip it rather than pick a wrong one.

Entity names differ between integrations and versions. Check yours under Developer tools,
States.

| Charger | Charger power | Cable connected |
|---|---|---|
| Wallbox (official integration) | Charging power (kW) | no binary sensor: template from the Status description sensor |
| Wallbox through a local gateway (MQTT and similar) | the gateway's charging power (W or kW) | its plugged in / cable binary sensor, if it has one |
| Zappi (myenergi) | the Zappi's own charging power, not the grid CT | template from the Plug status sensor |
| Easee | Power (kW) | template from the Status sensor |
| Ohme | Power | template from the Status sensor |
| OCPP | Power active import | template from the connector Status (example below) |
| Smart plug (wall socket charging) | the plug's power (W), only if nothing else is on it | skip |
| No home charger | skip, tick "I do not charge at home" | skip |

Chargers often go unavailable for a moment when Home Assistant restarts (MQTT gateways on
every restart). A charger power sensor that goes unavailable at home or with the cable in keeps
the charging state as it was for 10 minutes; after that the car's own data decides until it is
back. Right after a restart there is nothing to keep, so a home charge counts from the first
charger reading after it.

A charger that shows charging power as negative is fine: Smart Poll notices when a fresh car
reading says charging while the sensor shows at least the threshold below zero, flips it, and
remembers that. A reading at or above the threshold undoes it, and a sensor that has ever shown
one is never flipped. A grid or house meter that shows export as negative is not flipped
unless the car is charging at the same moment, but it is still the wrong sensor.

**Template cable sensor.** When the charger only has a status text sensor, make a template
binary sensor (Settings, Devices and services, Helpers, Template, Binary sensor, or YAML). This
one is for OCPP, where "Available" means no car is connected:

```yaml
template:
  - binary_sensor:
      - name: "Car cable connected"
        state: "{{ states('sensor.charger_status_connector') not in ['Available', 'Unavailable', 'Faulted'] }}"
        availability: "{{ has_value('sensor.charger_status_connector') }}"
```

For other chargers, replace the sensor and list the states it shows with no car plugged in
(for example `unplugged` on Ohme, `disconnected` on Easee, `EV Disconnected` on Zappi). Check
what yours shows by unplugging once.

**No home charger (public only).** Tick "I do not charge at home" and skip both sensors.
Then:

- a charge started within a few minutes of a drive is seen at one of the two just-parked polls
  (about 3 and 8 minutes after parking). Later than that, it is seen at the next parked poll
  (up to an hour by default). From then on it is polled per percent as usual;
- DC fast charging is usually short, so it is often over before an hourly poll would see it.
  The just-parked polls are what catch it;
- a DC charge with the car switched on counts as driving, which also polls every minute;
- "at home" comes from the car's last GPS fix in your home zone.

Public charging is also seen with a home charger set up: away from home with the home cable
not in, the car's own charging state counts once a poll finds it charging.

Known limit: a car that reports charging as negative battery power and has no charging flag
entity is not seen charging from its own data. It needs a charger power sensor.

## Charging

Energy is added up from the charger's power (or the car's own battery power from its last
reading). The car only reports whole percent, so each reading narrows down where the last tick
was, and the next poll is due when about one percent's worth has gone in since.

Energy per 1% is learned separately below and above the band edge. Each finished charge moves
it in proportion to how much it covered: a charge of 40 % or more moves it half way to what it
measured, a short top-up much less.

The car's own battery power is also used when no charger reading is there, whichever sign the
car uses for charging (learned from its readings and kept).

**Shared charger or meter.** If the charger sensor shows power while the car says it is not
charging and its battery level does not rise, over about two percent's worth of energy and two
fresh car readings, it is ignored and Repairs says so ("charger does not seem to be charging
this car"). Short dips in power do not reset this. It is used again once it is seen charging
this car, or when another sensor is picked. With the car full (99 % or more), or with the cable
sensor saying plugged in (the car at its limit heating or cooling on the cable), it is only
ignored, with no Repairs issue, until it has been idle for 15 minutes, the car says charging,
or the battery level rises (for a full car, drops below full).

At home an idle charger is final, so a paused home charge (solar, a schedule) does not keep
polling. A charge seen only in the car's data (public charging, or no charger sensor) is taken
as over once the car's newest reading is 30 minutes old and a charge poll since brought
nothing newer.

## Two drivers, two cars

**Two drivers.** Pick both phones at "Who drives it?". Android phones go in phone Bluetooth
sensors, iPhones use a helper each (or share one) as wake hints. The car Bluetooth list merges
the paired devices of all chosen phones, so the car appears once.

**More than one BYD.** Add the integration once per car. Each one picks its own car Bluetooth
device, so a phone connecting to the other car does not wake this one.

- With Android phones, pick each car's own entry in the car Bluetooth list. If both have BYD
  in their name, check the MAC.
- Wake hints are not tied to a car. Android Auto turns on in either car, so it starts fast
  polling on every car it is set on. That costs a few polls, nothing more. An iPhone
  Shortcut is tied to the Bluetooth device it was made for, so give each car its own helper.
- **One charger per car.** A charger both cars use cannot be told apart: when the other car
  charges, this one sees charging power while it is not charging. Smart Poll notices, ignores
  the charger and raises "charger does not seem to be charging this car" until it is seen
  charging this car again. A shared cable sensor is flagged too ("cable sensor does not match
  the car") when this car drives off while the other is plugged in. If both cars use one
  charger, set it only on the car that uses it most, or on neither.

## Diagnostics

Diagnostic entities on the Smart Poll device:

- **Car polls (24 h)**: readings that reached the car in the last day, including BYD Vehicle's
  own.
- **Parked awake share**: share of parked time in the last 24 h that the car was found drawing
  power from its battery (awake). Each parked reading counts for the time until the next one,
  at most the longest parked interval. An awake reading followed by a sleeping one (or none
  yet) counts for at most 15 minutes, so the readings just after a drive do not count as hours
  awake.
- **Parked drain**: battery percent lost per day over parked stretches in the last 72 h,
  charging left out. It needs 12 h of parked readings before it shows a value.
- **Energy per 1%**: the kWh per percent in use for the current battery level. Attributes
  `below_band_edge`, `above_band_edge` and `band_edge` show both learned values.
- **Car data stale**: on after two of Smart Poll's own polls in a row bring back the car's
  previous reading (same timestamp), while driving or charging. Polls while parked or
  expecting a drive, BYD Vehicle's own timer and other integrations do not count. The count
  starts from zero at each new drive or charge (a drive straight into a charge counts as one).
  Any new reading turns it off. A charge poll that brings nothing new already backs off the
  next one (2 min doubling to 30 min). While it is on, the **Estimated battery level** keeps counting from the charger's energy past the
  car's last whole percent (its `source` attribute says so). Attribute `stale_refreshes` is
  the count.

**Tuning the parked at home rate.** It starts at 1 hour, like away. A longer rate makes sense
when the signals at home cover what polls would otherwise catch:

1. Check `signals_in_use` lists charger power and cable connected, plus phone bluetooth or wake
   hints for every driver.
2. Note parked drain and parked awake share after a few normal days.
3. Set "Parked at home" to 2 to 4 hours and wait another 3 days.
4. If parked drain went down and `last_missed_drive` has not changed, keep it. If drives get
   missed, a driver's phone signal is not working. Fix that or go back.

If the awake share stays high however long the interval, polls are probably not what keeps the
car awake, and a longer rate will not save much.

## Repairs

Smart Poll raises these under Settings, Repairs. Each goes away on its own once fixed.

| Issue | What it means | What to do |
|---|---|---|
| No charger power sensor | no charger sensor and "I do not charge at home" not ticked | add the charger's power sensor under Configure, or tick "I do not charge at home" |
| Charger does not seem to be charging this car | the charger showed power while the car was not charging and its level did not rise | pick a sensor that only shows this car (see [Chargers](#chargers)); clears once it is seen charging this car |
| Cable sensor does not match the car | the cable sensor said plugged in while the car was on for 15 min, or still moving 10 min after it first moved | it is ignored until it next turns off; remove it if it keeps happening (stuck, or a shared charger) |
| (entity) not found / is disabled / is unavailable | a configured signal was removed, disabled, or unavailable for 6 hours | pick it again, enable it, or check its device |
| Phone Bluetooth sensor is off | the phone's Bluetooth connection sensor is disabled | turn it on in the companion app, Manage sensors |
| Car not found in BYD Vehicle | after 3 setup tries in a row the car was not in BYD Vehicle | add BYD Vehicle again with the same account (the car is found by VIN), or remove Smart Poll for a car that is gone |
| BYD Vehicle entities missing / disabled | after 3 setup tries the poll interval numbers, battery level or Vehicle on sensor were missing or disabled | update BYD Vehicle to 0.0.88 or newer, or enable them on the car's device page |

An old BYD Vehicle (before 0.0.88) is not a Repairs issue: setup stops, and the integration
card shows "Needs BYD Vehicle 0.0.88 or newer, and X is installed" until BYD Vehicle is updated
and Home Assistant restarted.
