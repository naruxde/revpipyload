# -*- coding: utf-8 -*-
"""Watchdog systems to monitor plc program and reset_driver of piCtory."""
__author__ = "Sven Sager"
__copyright__ = "Copyright (C) 2020 Sven Sager"
__license__ = "GPLv3"

import os
from fcntl import ioctl
from threading import Thread

import proginit as pi


class ResetDriverWatchdog(Thread):
    """Watchdog to catch a piCtory reset_driver action."""

    def __init__(self):
        super(ResetDriverWatchdog, self).__init__()
        self.daemon = True
        self._exit = False
        self._fh = None
        self._triggered = False
        self.start()

    def run(self) -> None:
        """
        Mainloop of watchdog for reset_driver.

        If the thread can not open the process image or the IOCTL is not
        implemented (wheezy), the thread function will stop. The trigger
        property will always return True.
        """
        pi.logger.debug("enter ResetDriverWatchdog.run()")

        try:
            self._fh = os.open(pi.pargs.procimg, os.O_RDONLY)
        except Exception:
            pi.logger.error(
                "can not open process image at '{0}' for piCtory "
                "reset_driver watchdog".format(pi.pargs.procimg)
            )
            return

        # The ioctl will return 2 byte (c-type int)
        byte_buff = bytearray(2)
        while not self._exit:
            try:
                rc = ioctl(self._fh, 19250, byte_buff)
                if rc == 0 and byte_buff[0] == 1:
                    self._triggered = True
                    pi.logger.debug("piCtory reset_driver detected")
            except Exception:
                os.close(self._fh)
                self._fh = None
                pi.logger.warning("IOCTL KB_WAIT_FOR_EVENT is not implemented")
                return

        pi.logger.debug("leave ResetDriverWatchdog.run()")

    def stop(self) -> None:
        """Stop watchdog for piCtory reset_driver."""
        pi.logger.debug("enter ResetDriverWatchdog.stop()")

        self._exit = True
        if self._fh is not None:
            os.close(self._fh)
            self._fh = None

        pi.logger.debug("leave ResetDriverWatchdog.stop()")

    @property
    def triggered(self) -> bool:
        rc = self._triggered or not self.is_alive()
        self._triggered = False
        return rc
