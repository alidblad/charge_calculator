from __future__ import annotations

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.typing import ConfigType

# The domain of your component. Should be equal to the name of your component.
DOMAIN = "charge_calculator"

CONF_TOPIC = 'topic'
DEFAULT_TOPIC = 'home-assistant/mqtt_example'

# Schema to validate the configured MQTT topic
CONFIG_SCHEMA = vol.Schema({
    vol.Optional(CONF_TOPIC, default=DEFAULT_TOPIC): cv.string
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MQTT async example component."""
    topic = config[DOMAIN][CONF_TOPIC]
    entity_id = 'charge_calculator.last_message'

    hass.states.async_set(entity_id, topic)

    # Service to publish a message on MQTT.
    @callback
    def set_state_service(call: ServiceCall) -> None:
        """Service to send a message."""
        
        entity_id = 'charge_calculator.last_message'
        result_state = call.data.get(entity_id, "default")
        hass.states.async_set(entity_id, result_state)        

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'set_state', set_state_service)

    # Return boolean to indicate that initialization was successfully.
    return True