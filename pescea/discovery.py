"""Escea device discovery."""

import asyncio

from async_timeout import current_task, timeout
import logging
from abc import abstractmethod, ABC
from asyncio import (AbstractEventLoop, Condition, Future, Task, )
from logging import Logger
from typing import Dict, List, Set, Optional

from .controller import Controller
from .datagram import FireplaceDatagram
from .message import FireplaceMessage

DISCOVERY_SLEEP = 60.0  # Interval between status refreshes
DISCOVERY_RESCAN = 5.0  # Interval on a Controller losing comms

BROADCAST_IP_ADDR = '255.255.255.255'

_LOG = logging.getLogger('pescea.discovery')  # type: Logger


class LogExceptions:
    """Utility context manager to log and discard exceptions"""

    def __init__(self, func: str) -> None:
        self.func = func

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            _LOG.exception(
                "Exception ignored when calling listener %s", self.func)
        return True


class Listener:
    """Base class for listeners for Escea updates"""

    def controller_discovered(self, ctrl: Controller) -> None:
        """
        New controller discovered. This will also be called for all
        existing controllers if a new listener is registered
        """

    def controller_disconnected(self, ctrl: Controller, ex: Exception) -> None:
        """
        Connection lost to controller. Exception argument will show reason why.
        """

    def controller_reconnected(self, ctrl: Controller) -> None:
        """
        Reconnected to controller.
        """

    def controller_update(self, ctrl: Controller) -> None:
        """Called when a system update message is received from the controller.
        Controller data will be set to new value.
        """


class AbstractDiscoveryService(ABC):
    """Interface for discovery.

    This service is both a context manager, and an asynchronous context
    manager. When used in the context manager version, the start
    discovery and close will be called automatically when opening
    and closing the context respectively.
    """

    @abstractmethod
    def add_listener(self, listener: Listener) -> None:
        """Add a listener.

        All existing controllers will be passed to the listener."""

    @abstractmethod
    def remove_listener(self, listener: Listener) -> None:
        """Remove a listener"""

    @abstractmethod
    async def start_discovery(self) -> None:
        """Async version to start discovery.
        Will return once discovery is started, but before any controllers
        are found.
        """

    @abstractmethod
    async def rescan(self) -> None:
        """Trigger rescan for new controllers / update IP addresses of
        existing controllers.

        Returns immediately, listener will be called with any new
        controllers or if reconnected.
        """

    @abstractmethod
    async def close(self) -> None:
        """Stop discovery.

        As these are all UDP comms, there are no open connections to close.

        Returns immediately, but closing off controllers may take time
        """

    @property
    def is_closed(self) -> bool:
        """Return true if closed"""

    @property
    def controllers(self) -> Dict[str, Controller]:
        """Dictionary of all the currently discovered controllers"""


class DiscoveryService(AbstractDiscoveryService, Listener):
    """Discovery protocol class. Not for external use."""

    def __init__(self, loop: AbstractEventLoop = None, ip_addr: str = BROADCAST_IP_ADDR) -> None:
        """Start the discovery protocol using the supplied loop.

        raises:
            RuntimeError: If attempted to start the protocol when it is
                          already running.
        """
        self._controllers = {}  # type: Dict[str, Controller]
        self._disconnected = set()  # type: Set[str]
        self._listeners = []  # type: List[Listener]
        self._close_task = None  # type: Optional[Task]

        _LOG.info("Starting discovery protocol")
        if not loop:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop

        self._broadcast_ip = ip_addr
        self._datagram = FireplaceDatagram(self, ip_addr)

        self._scan_condition = Condition(loop=self.loop)  # type: Condition

        self._tasks = []  # type: List[Future]

    # Async context manager interface
    async def __aenter__(self) -> AbstractDiscoveryService:
        await self.start_discovery()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    def _task_done_callback(self, task: Task):
        if task.exception():
            _LOG.exception("Uncaught exception", exc_info=task.exception())
        self._tasks.remove(task)

    # managing the task list.
    def create_task(self, coro) -> Task:
        """Create a task in the event loop. Keeps track of created tasks."""
        task = self.loop.create_task(coro)  # type: Task
        self._tasks.append(task)

        task.add_done_callback(self._task_done_callback)
        return task

    # Listeners.
    def add_listener(self, listener: Listener) -> None:
        """Add a discovered listener.

        All existing controllers will be passed to the listener."""
        self._listeners.append(listener)

        def callback():
            for controller in self._controllers.values():
                listener.controller_discovered(controller)
        self.loop.call_soon(callback)

    def remove_listener(self, listener: Listener) -> None:
        """Remove a listener"""
        self._listeners.remove(listener)

    def controller_discovered(self, ctrl: Controller) -> None:
        _LOG.info(
            "New controller found: id=%s ip=%s",
            ctrl.device_uid, ctrl.device_ip)
        for listener in self._listeners:
            with LogExceptions("controller_discovered"):
                listener.controller_discovered(ctrl)

    def controller_disconnected(self, ctrl: Controller, ex: Exception) -> None:
        _LOG.warning(
            "Connection to controller lost: id=%s ip=%s",
            ctrl.device_uid, ctrl.device_ip)
        self._disconnected.add(ctrl.device_uid)
        self.loop.create_task(self._rescan())
        for listener in self._listeners:
            with LogExceptions("controller_disconnected"):
                listener.controller_disconnected(ctrl, ex)

    def controller_reconnected(self, ctrl: Controller) -> None:
        _LOG.warning(
            "Controller reconnected: id=%s ip=%s",
            ctrl.device_uid, ctrl.device_ip)
        self._disconnected.remove(ctrl.device_uid)
        for listener in self._listeners:
            with LogExceptions("controller_reconnected"):
                listener.controller_reconnected(ctrl)

    def controller_update(self, ctrl: Controller) -> None:
        for listener in self._listeners:
            with LogExceptions("controller_update"):
                listener.controller_update(ctrl)

    @property
    def controllers(self) -> Dict[str, Controller]:
        """Dictionary of all the currently discovered controllers"""
        return self._controllers

    # Non-context versions of starting.
    async def start_discovery(self) -> None:
        await self._send_broadcast()

    async def _scan_loop(self) -> None:
        while True:
            await self._send_broadcast()

            try:
                async with timeout(
                        DISCOVERY_RESCAN if self._disconnected
                        else DISCOVERY_SLEEP):
                    async with self._scan_condition:
                        await self._scan_condition.wait()
            except asyncio.TimeoutError:
                pass

            if self._close_task:
                return

    async def _send_broadcast(self):
        _LOG.debug("Sending discovery message to addr %s", self._broadcast_ip)
        try:
            responses = await self._datagram._send_command_async(
                FireplaceMessage.CommandID.SEARCH_FOR_FIRES)
            for addr in responses:
                self._discovery_received(responses[addr], addr)
        except (asyncio.TimeoutError) as ex:
            raise ConnectionError("No controllers responded") \
                from ex

    async def rescan(self) -> None:
        _LOG.debug("Manual rescan of controllers triggered.")
        await self._rescan()

    async def _rescan(self) -> None:
        async with self._scan_condition:
            self._scan_condition.notify()

    # Closing the connection
    async def close(self) -> None:
        _LOG.info("Close called on discovery service.")
        await asyncio.wait(self._tasks)

    def error_received(self, exc):
        _LOG.warning(
            "Error passed and ignored to error_received: %s",
            repr(exc))

    def _find_by_addr(self, addr: str) -> Optional[Controller]:
        for _, ctrl in self._controllers.items():
            if ctrl.device_ip == addr[0]:
                return ctrl
        return None

    async def _wrap_update(self, coro):
        try:
            await coro
        except ConnectionError as ex:
            _LOG.warning(
                "Unable to complete %s due to connection error: %s",
                coro, repr(ex))

    def _discovery_received(self, data, addr):
        device_ip, _ = addr
        device_uid = data.serial_number

        if device_uid not in self._controllers:
            # Create new controller.
            # We don't have to set the loop here since it's set for
            # the thread already.
            controller = self._create_controller(device_uid, device_ip)

            async def initialize_controller():
                try:
                    await controller._initialize()
                except ConnectionError as ex:
                    _LOG.warning(
                        "Can't connect to discovered server at IP '%s'"
                        " exception: %s", device_ip, repr(ex))
                    return

                self._controllers[device_uid] = controller
                self.controller_discovered(controller)

            self.create_task(initialize_controller())
        else:
            controller = self._controllers[device_uid]
            controller._refresh_address(device_ip)

    def _create_controller(self, device_uid, device_ip):
        return Controller(
            self, device_uid=device_uid, device_ip=device_ip)

    """ Following methods are for test purposes only """

    def dump(self, indent: str = '') -> None:
        tab = "    "
        print(indent + "DiscoveryService:")
        print(indent + tab + "Controllers: {0}".format(self._controllers))
        print(indent + tab + "Disconnected: {0}".format(self._disconnected))
        print(indent + tab + "Listeners: {0}".format(self._listeners))
        if self._close_task is not None:
            print(indent + tab + "Close Task: {0}".format(self._close_task))
        print(indent + tab + "Broadcast IP: {0}".format(self._broadcast_ip))
        self._datagram.dump(indent=indent + tab)
        print(indent + tab +
              "Scan Condition: {0}".format(self._scan_condition))
        print(indent + tab + "Tasks: {0}".format(self._tasks))


def discovery(*listeners: Listener,
              loop: AbstractEventLoop = None,
              ip_addr: str = None) -> AbstractDiscoveryService:
    """Create discovery service. Returned object is an asynchronous
    context manager so can be used with 'async with' statement.
    Alternately call start_discovery or start_discovery_async to commence
    the discovery process."""
    service = DiscoveryService(loop=loop, ip_addr=ip_addr)
    for listener in listeners:
        service.add_listener(listener)
    return service
