"""Escea Network Controller module"""

import logging
import asyncio
import sys

from enum import Enum
from typing import Dict, Union
from time import time
from async_timeout import timeout
from copy import deepcopy

# Pescea imports:
from pescea.message import FireplaceMessage, CommandID, ResponseID, MIN_SET_TEMP, MAX_SET_TEMP, expected_response
from pescea.datagram import FireplaceDatagram

_LOG = logging.getLogger('pescea.controller')

# Time to wait for results from UDP command to server
REQUEST_TIMEOUT = 5

# Seconds between updates under normal conditions
#  - nothing changes quickly with fireplaces
REFRESH_INTERVAL = 30.0

# Seconds to wait between discovery notifications (if no change)
NOTIFY_REFRESH_INTERVAL = 5*60.0

# Retry rate when first get disconnected
RETRY_INTERVAL = 10.0

# Timeout to stop retrying and reduce poll rate (and notify Discovery)
RETRY_TIMEOUT = 60.0

# Time to wait when have been disconnected longer than RETRY_TIMOUT
DISCONNECTED_INTERVAL = 5*60.0

# Time to wait for fireplace to start up / shut down
# - Commands are stored, but not sent to the fireplace until it has settled
# Disclaimer: This was measured from Escea remote display
ON_OFF_BUSY_WAIT_TIME = 66

class ControllerState(Enum):
    """ Controller states:

        Under normal operations:
            The Controller is READY:
                - The Controller sends commands directly to the Fireplace
                - The Controller polls at REFRESH_INTERVAL
        When toggling the fire power:
            The Controller remains BUSY for ON_OFF_BUSY_WAIT_TIME:
                - The Controller buffers requests but does not send to the Fireplace
        When responses are missed (expected as we are using UDP datagrams):
            The Controller is NON_RESPONSIVE
                - The Controller will poll at a (quicker) retry rate
        When there are no comms for a prolonged period:
            The Controller enters DISCONNECTED state
                - The Controller will continue to poll at a reduced rate
                - The Controller buffers requests but cannot send to the Fireplace
    """
    BUSY = 'BusyWaiting'
    READY = 'Ready'
    NON_RESPONSIVE = 'NonResponsive'
    DISCONNECTED = 'Disconnected'

class Fan(Enum):
    """All fan modes"""
    FLAME_EFFECT = 'FlameEffect'
    AUTO = 'Auto'
    FAN_BOOST = 'FanBoost'

class DictEntries(Enum):
    """Available controller attributes - Internal Use Only"""
    IP_ADDRESS = 'IPAddress'
    DEVICE_UID = 'DeviceUId'
    CONTROLLER_STATE = 'ControllerState'
    HAS_NEW_TIMERS = 'HasNewTimers'
    FIRE_IS_ON = 'FireIsOn'
    FAN_MODE = 'FanMode'
    DESIRED_TEMP = 'DesiredTemp'
    CURRENT_TEMP = 'CurrentTemp'

class Controller:
    """Interface to Escea controller"""

    DictValue = Union[str, int, float, bool, Fan]
    ControllerData = Dict[DictEntries, DictValue]

    def __init__(self, discovery, device_uid: str,
                 device_ip: str) -> None:
        """Create a controller interface.

        Usually this is called from the discovery service.

        Args:
            device_uid: Controller UId as a string (Serial Number of unit)
            device_addr: Device network address. Usually specified as IP
                address
        """


        """ System settings:
            on / off
            fan mode
            set temperature
            current temperature
        """
        self._discovery = discovery
        self._system_settings = {}  # type: Controller.ControllerData
        self._prior_settings = {} # type: Controller.ControllerData        
        self._system_settings[DictEntries.IP_ADDRESS] = device_ip
        self._system_settings[DictEntries.DEVICE_UID] = device_uid

        self._datagram = FireplaceDatagram(self._discovery.loop, device_ip, self._discovery.sending_lock)

        self._loop_interrupt_condition = asyncio.Condition(loop=self._discovery.loop)

        self._initialised = False        

    async def initialize(self) -> None:
        """ Initialize the controller, does not complete until the firplace has
            been contacted and current settings read.
        """

        # Under normal operations, the Controller state is READY
        self._state = ControllerState.READY
        self._last_response = 0.0 # Used to track last valid message received
        self._busy_end_time = 0.0 # Used to track when exit BUSY state
        self._last_update = 0.0 # Used to rate limit the notifications to discovery

        # Use to exit poll_loop when told to
        self._closed = False

        # Read current state of fireplace
        await self._refresh_system(notify=False)

        self._initialised = True

        # Start regular polling for status updates
        self._discovery.loop.create_task(self._poll_loop())

    async def close(self):
        """Signal loop to exit"""
        self._closed = True
        async with self._loop_interrupt_condition:
            self._loop_interrupt_condition.notify()

    async def _poll_loop(self) -> None:
        """ Regularly poll for status update from fireplace.
            If Disconnected, retry based on how long ago we last had an update.
            If Disconnected for a long time, let Discovery know we are giving up.
        """
        while not self._closed:

            try:
                await self._refresh_system()
            except:
                exc = sys.exc_info()[0]
                _LOG.exception('Unexpected error: %s - EXITING', exc)
                self._closed = True
                return

            _LOG.debug('Polling unit %s at address %s (current state is %s)',
                self._system_settings[DictEntries.DEVICE_UID],
                self._system_settings[DictEntries.IP_ADDRESS],
                self._state)

            if self._state == ControllerState.READY:
                sleep_time = REFRESH_INTERVAL
            elif self._state == ControllerState.NON_RESPONSIVE:
                sleep_time = RETRY_INTERVAL
            elif self._state == ControllerState.DISCONNECTED:
                sleep_time = DISCONNECTED_INTERVAL
            elif self._state == ControllerState.BUSY:
                sleep_time = max(self._busy_end_time - time(), 0.0)

            try:
                # Sleep for poll time, allow early wakeup
                async with timeout(sleep_time):
                    async with self._loop_interrupt_condition:
                        await self._loop_interrupt_condition.wait()
            except asyncio.TimeoutError:
                pass

    @property
    def device_ip(self) -> str:
        """IP Address of the unit"""
        return self._system_settings[DictEntries.IP_ADDRESS]

    @property
    def device_uid(self) -> str:
        """UId of the unit (serial number)"""
        return self._system_settings[DictEntries.DEVICE_UID]

    @property
    def discovery(self):
        return self._discovery

    @property
    def state(self) -> ControllerState:
        """True if the system is turned on"""
        return self._state

    @property
    def is_on(self) -> bool:
        """True if the system is turned on"""
        return self._get_system_state(DictEntries.FIRE_IS_ON)

    async def set_on(self, value: bool) -> None:
        """Turn the system on or off.
           Async method, await to ensure command revieved by system.
           Note: After systems receives on or off command, must wait several minutes to be actioned
        """
        await self._set_system_state(DictEntries.FIRE_IS_ON, value)

    @property
    def fan(self) -> Fan:
        """The current fan level."""
        return self._get_system_state(DictEntries.FAN_MODE)

    async def set_fan(self, value: Fan) -> None:
        """The fan level. 
           Async method, await to ensure command revieved by system.
        """
        await self._set_system_state(DictEntries.FAN_MODE, value)

    @property
    def desired_temp(self) -> float:
        """fireplace DesiredTemp temperature.
        """
        return float(self._get_system_state(DictEntries.DESIRED_TEMP))

    async def set_desired_temp(self, value: float):
        """Fireplace DesiredTemp temperature.

            This is the unit target temp
            Args:
                value: Valid settings are in range MIN_TEMP..MAX_TEMP
                at 1 degree increments (will be rounded)
        """
        degrees = round(value)
        if degrees < MIN_SET_TEMP or degrees > MAX_SET_TEMP:
            _LOG.error('Desired Temp %s is out of range (%s-%s)', degrees, MIN_SET_TEMP, MAX_SET_TEMP)
            return

        await self._set_system_state(DictEntries.DESIRED_TEMP, degrees)

    @property
    def current_temp(self) -> float:
        """The room air temperature"""
        return float(self._get_system_state(DictEntries.CURRENT_TEMP))

    @property
    def min_temp(self) -> float:
        """The minimum valid target (desired) temperature"""
        return float(MIN_SET_TEMP)

    @property
    def max_temp(self) -> float:
        """The maximum valid target (desired) temperature"""
        return float(MAX_SET_TEMP)

    async def _refresh_system(self, notify: bool = True) -> None:
        """ Request fresh status from the fireplace.

            This is also where state changes are handled
            Approach:

                if current state BUSY (not timed out) -> return

                request status

                New status received:
                    if prior state READY
                        update local system settings from received message
                    else (prior state must be DISCONNECTED / NON_RESPONSIVE / BUSY (timeout))
                        sync buffered commands to fireplace
                        new state READY
                        if prior state DISCONNECTED:
                            notify discovery reconnected

                No status received:
                    prior state *ANY*
                        if time since last response < RETRY_TIMEOUT
                            new state NON_RESPONSIVE
                        else
                            notify discovery disconnected
                            new state DISCONNECTED
        """
        if self._state == ControllerState.BUSY and time() < self._busy_end_time:
            return

        prior_state = self._state
        response = await self._request_status()
        if (response is not None) and (response.response_id == ResponseID.STATUS):
            # We have a valid response - the controller is communicating

            self._state = ControllerState.READY

            # These values are readonly, so copy them in any case
            self._system_settings[DictEntries.HAS_NEW_TIMERS] = response.has_new_timers
            self._system_settings[DictEntries.CURRENT_TEMP]   = response.current_temp

            if prior_state == ControllerState.READY:

                # Normal operation, update our internal values
                self._system_settings[DictEntries.DESIRED_TEMP]   = response.desired_temp
                if response.fan_boost_is_on:
                    self._system_settings[DictEntries.FAN_MODE]   = Fan.FAN_BOOST
                elif response.flame_effect:
                    self._system_settings[DictEntries.FAN_MODE]   = Fan.FLAME_EFFECT
                else:
                    self._system_settings[DictEntries.FAN_MODE]   = Fan.AUTO
                self._system_settings[DictEntries.FIRE_IS_ON]     = response.fire_is_on

            else:

                # We have come back to READY state.
                # We need to try to sync the fireplace settings with our internal copies


                if response.desired_temp != self._system_settings[DictEntries.DESIRED_TEMP]:
                    await self._set_system_state(DictEntries.DESIRED_TEMP, self._system_settings[DictEntries.DESIRED_TEMP], sync=True)

                if response.fan_boost_is_on:
                    response_fan = Fan.FAN_BOOST
                elif response.flame_effect:
                    response_fan = Fan.FLAME_EFFECT
                else:
                    response_fan = Fan.AUTO
                if response_fan != self._system_settings[DictEntries.FAN_MODE]:
                    await self._set_system_state(DictEntries.FAN_MODE, self._system_settings[DictEntries.FAN_MODE], sync=True)

                # Do power last
                if response.fire_is_on != self._system_settings[DictEntries.FIRE_IS_ON]:
                    # This will also set the BUSY state
                    await self._set_system_state(DictEntries.FIRE_IS_ON, self._system_settings[DictEntries.FIRE_IS_ON], sync=True )

                if prior_state == ControllerState.DISCONNECTED:
                    self._discovery.controller_reconnected(self)
            
            if notify:
                changes_found = False
                for entry in self._system_settings:
                    if not entry in self._prior_settings \
                            or (self._prior_settings[entry] != self._system_settings[entry]):
                        changes_found = True
                        break
                if changes_found \
                        or (time() - self._last_update > NOTIFY_REFRESH_INTERVAL):
                    self._last_update = time()
                    self._prior_settings = deepcopy(self._system_settings)
                    self._discovery.controller_update(self)       
        else:
            # No / invalid response, need to check if we need to change state
            if time() - self._last_response < RETRY_TIMEOUT:
                self._state = ControllerState.NON_RESPONSIVE
            else:
                self._state = ControllerState.DISCONNECTED
                if prior_state != ControllerState.DISCONNECTED:
                    self._discovery.controller_disconnected(self, TimeoutError)

    async def _request_status(self) -> FireplaceMessage:
        try:
            responses = await self._datagram.send_command(CommandID.STATUS_PLEASE)
            if (len(responses) > 0):
                this_response = next(iter(responses)) # only expecting one
                if responses[this_response].response_id == expected_response(CommandID.STATUS_PLEASE):
                    # all good
                    self._last_response = time()
                    return responses[this_response]
        except ConnectionError:
            pass
        # If we get here... did not receive a response or not valid
        if self._state != ControllerState.DISCONNECTED:
            self._state = ControllerState.NON_RESPONSIVE
        return None

    def refresh_address(self, address):
        """Called from discovery to update the address"""
        if self._system_settings[DictEntries.IP_ADDRESS] == address:
            return

        self._datagram.set_ip(address)
        self._system_settings[DictEntries.IP_ADDRESS] = address

        async def signal_loop(self):
            async with self._loop_interrupt_condition:
                self._loop_interrupt_condition.notify()

        self._discovery.loop.create_task(signal_loop(self))

    def _get_system_state(self, state: DictEntries):
        return self._system_settings[state]

    async def _set_system_state(self, state: DictEntries, value, sync: bool = False):

        # ignore if we are not forcing the synch, and value matches already
        if (not sync) and (self._system_settings[state] == value):
            return

        _LOG.debug('_set_system_state - uid: %s | %s from:%s to:%s  (sync:%s)',   
                str(self.device_uid),
                str(state),
                str(self._system_settings[state]),
                str(value),
                str(sync))   

        # save the new value internally
        self._system_settings[state] = value

        if (not sync) and (self._state != ControllerState.READY):
            # We've saved the new value.... just can't send it to the controller yet
            return

        command = None

        if state == DictEntries.FIRE_IS_ON:
            if value:
                command = CommandID.POWER_ON
            else:
                command = CommandID.POWER_OFF

        elif state == DictEntries.DESIRED_TEMP:
            command = CommandID.NEW_SET_TEMP

        elif state == DictEntries.FAN_MODE:

            # Fan is implemented via separate FLAME_EFFECT and FAN_BOOST commands
            # Any change will take one or two separate commands:
            # PART 1 -
            #
            # To AUTO:
            # 1. turn off FAN_BOOST
            if value == Fan.AUTO:
                command = CommandID.FAN_BOOST_OFF

            # To FAN_BOOST:
            # 1. Turn off FLAME_EFFECT
            elif value == Fan.FAN_BOOST:
                command = CommandID.FLAME_EFFECT_OFF

            # To FLAME_EFFECT:
            # 1. Turn off FAN_BOOST
            elif value == Fan.FLAME_EFFECT:
                command = CommandID.FAN_BOOST_OFF

        else:
            raise(AttributeError, 'Unexpected state: {0}'.format(state))

        if command is not None:
            valid_response = False
            try:
                responses = await self._datagram.send_command(command, value)
                if (len(responses) > 0) \
                    and (responses[next(iter(responses))].response_id == expected_response(command)):
                        # No / invalid response
                        valid_response = True
                        _LOG.debug('_set_system_state - send_command(success): %s -> %s',   
                                str(self.device_uid),
                                str(command))                           

            except ConnectionError:
                pass
            if valid_response:
                self._last_response = time()
            else:
                return

        if state == DictEntries.FAN_MODE:
            # Fan is implemented via separate FLAME_EFFECT and FAN_BOOST commands
            # Any change will take one or two separate commands:
            # PART 2 -
            #
            # To AUTO:
            # 2. turn off FLAME_EFFECT
            if value == Fan.AUTO:
                command = CommandID.FLAME_EFFECT_OFF
                            
            # To FAN_BOOST:
            # 2. Turn on FAN_BOOST
            elif value == Fan.FAN_BOOST:
                command = CommandID.FAN_BOOST_ON

            # To FLAME_EFFECT:
            # 2. Turn on FLAME_EFFECT
            else:
                command = CommandID.FLAME_EFFECT_ON

            valid_response = False
            try:
                responses = await self._datagram.send_command(command, value)
                if (len(responses) > 0) \
                    and (responses[next(iter(responses))].response_id == expected_response(command)):
                        # No / invalid response
                        valid_response = True
                        _LOG.debug('_set_system_state - send_command(success): %s -> %s',   
                                str(self.device_uid),
                                str(command))                       
            except ConnectionError:
                pass
            if valid_response:
                self._last_response = time()
            else:
                return

        # Need to refresh immediately after setting (unless synching, then poll loop will update)
        if not sync:
            await self._refresh_system()

        # If get here, and just toggled the fireplace power... need to wait for a while
        if state == DictEntries.FIRE_IS_ON:
            self._state = ControllerState.BUSY
            self._busy_end_time = time() + ON_OFF_BUSY_WAIT_TIME