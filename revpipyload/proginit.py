# -*- coding: utf-8 -*-
"""Main functions of our program."""
import logging
import sys
from argparse import ArgumentParser
from configparser import ConfigParser
from os import fork as osfork
from os.path import exists as ospexists


class ProgInit():

    """Programmfunktionen fuer Parameter und Logger."""

    def __del__(self):
        """Clean up program."""
        # Logging beenden
        logging.shutdown()

    def __init__(self):
        """Initialize general program functions."""

        # Command arguments
        parser = ArgumentParser(
            description="RevolutionPi Python3 Loader"
        )
        parser.add_argument(
            "-d", "--daemon", action="store_true", dest="daemon",
            help="Run program as a daemon in background"
        )
        parser.add_argument(
            "-c", "--conffile", dest="conffile",
            default="/etc/revpipyload/revpipyload.conf",
            help="Application configuration file"
        )
        parser.add_argument(
            "-f", "--logfile", dest="logfile",
            help="Save log entries to this file"
        )
        parser.add_argument(
            "-v", "--verbose", action="count", dest="verbose",
            help="Switch on verbose logging"
        )
        self.pargs = parser.parse_args()

        # Prüfen ob als Daemon ausgeführt werden soll
        self.pidfile = "/var/run/revpipyload.pid"
        self.pid = 0
        if self.pargs.daemon:
            # Prüfen ob daemon schon läuft
            if ospexists(self.pidfile):
                raise SystemError(
                    "program already running as daemon. check {}".format(
                        self.pidfile
                    )
                )
            else:
                self.pid = osfork()
                if self.pid > 0:
                    with open(self.pidfile, "w") as f:
                        f.write(str(self.pid))
                    exit(0)

                # Ausgaben umhängen in Logfile
                sys.stdout = open("/var/log/revpipyload", "a")
                sys.stderr = sys.stdout

        # Initialize configparser globalconfig
        self.globalconffile = self.pargs.conffile
        self.globalconfig = ConfigParser()
        self.globalconfig.read(self.pargs.conffile)

        # Program logger
        self.logger = logging.getLogger()
        logformat = logging.Formatter(
            "{asctime} [{levelname:8}] {message}",
            datefmt="%Y-%m-%d %H:%M:%S", style="{"
        )
        lhandler = logging.StreamHandler(sys.stdout)
        lhandler.setFormatter(logformat)
        self.logger.addHandler(lhandler)
        if self.pargs.logfile is not None:
            lhandler = logging.FileHandler(filename=self.pargs.logfile)
            lhandler.setFormatter(logformat)
            self.logger.addHandler(lhandler)

        # Loglevel auswerten
        if self.pargs.verbose is None:
            loglevel = logging.WARNING
        elif self.pargs.verbose == 1:
            loglevel = logging.INFO
        elif self.pargs.verbose > 1:
            loglevel = logging.DEBUG
        self.logger.setLevel(loglevel)
