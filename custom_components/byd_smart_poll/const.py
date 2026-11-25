"""Constants for BYD Smart Poll."""

from __future__ import annotations

DOMAIN = "byd_smart_poll"
BYD_DOMAIN = "byd_vehicle"

CONF_DEVICE_ID = "device_id"
CONF_CHARGER_POWER = "charger_power"
CONF_CHARGER_CONNECTED = "charger_connected"
CONF_WAKE_HINTS = "wake_hints"
CONF_CAR_BLUETOOTH = "car_bluetooth"
CONF_PHONE_BLUETOOTH = "phone_bluetooth"
# In the entry data: a car Bluetooth name saved before whole names were needed (config entry
# version 1.1), which still matches as a part of the name until setup turns it into a MAC.
CONF_CAR_BLUETOOTH_PARTIAL = "car_bluetooth_partial"
# Ticked when the car is only charged away from home, so no charger sensor is expected.
CONF_NO_HOME_CHARGER = "no_home_charger"

CONF_PARKED_TELEMETRY = "parked_telemetry"
CONF_PARKED_HOME_TELEMETRY = "parked_home_telemetry"
CONF_PARKED_GPS = "parked_gps"
CONF_DRIVING_TELEMETRY = "driving_telemetry"
CONF_DRIVING_GPS = "driving_gps"
CONF_CHARGING_SAFETY = "charging_safety"
CONF_KWH_PER_PCT = "kwh_per_pct"
CONF_BATTERY_KWH = "battery_kwh"
CONF_KWH_BAND_EDGE = "kwh_band_edge"
CONF_CHARGING_MIN_POWER = "charging_min_power"
CONF_CHARGING_STOP_DELAY = "charging_stop_delay"
# In minutes in the options, seconds everywhere else.
CONF_WAKE_WINDOW = "wake_window"

# Config entry version 1.x. 1.3: a typed car Bluetooth name is the whole name, and
# "energy per 1%" is an override of the battery size. See async_migrate_entry.
ENTRY_MINOR_VERSION = 3

# Smart Poll needs BYD Vehicle at least this new: before it the poll intervals were per
# account rather than per car, and before 0.0.65 there were no interval numbers at all.
MIN_BYD_VERSION = "0.0.88"

# The BYD integration's number entities accept 30 s to 8 h.
MIN_INTERVAL = 30
MAX_INTERVAL = 28800

# Charging is counted from this much charger power (W). Below it the car is plugged in but
# idle, and draws from its own battery. The default for the option of the same name.
CHARGING_MIN_POWER_W = 300.0
# Solar charging dips briefly with clouds. Only count charging as stopped after this long.
# The default for the option of the same name.
CHARGING_STOP_DELAY = 120

# Never poll the car more often than this from here, whatever triggers it. Same as the
# driving rate: after unplugging, the next poll (a phone connecting) is the one that sees
# a drive start, so a longer gap only delays it.
MIN_POLL_GAP = 60
# How often the charge tracker is checked while charging.
CHARGE_CHECK_INTERVAL = 30
# After a wake hint (a phone connected to the car) poll at the driving rate for this long.
# The default for the option of the same name.
WAKE_WINDOW = 600

# Suffixes of the BYD integration's entity unique IDs ("<VIN><suffix>"). These are the same
# from BYD Vehicle 0.0.83 on (checked up to 0.0.93), unlike entity IDs which users can
# rename. Setup needs MIN_BYD_VERSION or newer anyway.
BYD_ENTITY_SUFFIXES = {
    "telemetry_interval": "_number_realtime_poll_interval",
    "gps_interval": "_number_gps_poll_interval",
    "polling_disabled": "_switch_disable_polling",
    "vehicle_on": "_realtime_vehicle_state",
    "charging": "_realtime_is_charging",
    "soc": "_realtime_elec_percent",
    "battery_power": "_realtime_battery_power",
    "tracker": "_tracker",
    "odometer": "_realtime_total_mileage",
    # Changes once per real reading from the car, so it counts polls that reached the car.
    "last_updated": "_realtime_last_updated",
    "gps_last_updated": "_gps_gps_last_updated",
}
# Without these the integration cannot work. The rest are used when present.
BYD_REQUIRED = ("telemetry_interval", "gps_interval", "vehicle_on", "soc")
