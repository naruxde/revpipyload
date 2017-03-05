# -*- coding: utf-8 -*-
"""Main functions of our program."""
import logging
import sys
from argparse import ArgumentParser
from configparser import ConfigParser
from os import fork as osfork
from os.path import exists as ospexists


logapp = "revpipyloadapp.log"
logplc = "revpipyload.log"
logger = None
pargs = None


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
            default="revpipyload.conf",
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
        global pargs
        pargs = parser.parse_args()

        # Prüfen ob als Daemon ausgeführt werden soll
        self.pidfile = "/var/run/revpipyload.pid"
        self.pid = 0
        if pargs.daemon:
            # Prüfen ob daemon schon läuft
            if ospexists(self.pidfile):
                raise SystemError(
                    "program already running as daemon. check {}".format(
                        self.pidfile
                    )
                )

            self.pid = osfork()
            if self.pid > 0:
                with open(self.pidfile, "w") as f:
                    f.write(str(self.pid))
                exit(0)

            global logapp
            global logplc

            # Ausgaben umhängen in Logfile
            logapp = "/var/log/revpipyloadapp"
            logplc = "/var/log/revpipyload"
            pargs.conffile = "/etc/revpipyload/revpipyload.conf"
            sys.stdout = open(logplc, "a")
            sys.stderr = sys.stdout

        # Initialize configparser globalconfig
        self.globalconffile = pargs.conffile
        self.globalconfig = ConfigParser()
        self.globalconfig.read(pargs.conffile)

        # Program logger
        global logger
        logger = logging.getLogger()
        logformat = logging.Formatter(
            "{asctime} [{levelname:8}] {message}",
            datefmt="%Y-%m-%d %H:%M:%S", style="{"
        )
        lhandler = logging.StreamHandler(sys.stdout)
        lhandler.setFormatter(logformat)
        logger.addHandler(lhandler)
        if pargs.logfile is not None:
            lhandler = logging.FileHandler(filename=pargs.logfile)
            lhandler.setFormatter(logformat)
            logger.addHandler(lhandler)

        # Loglevel auswerten
        if pargs.verbose is None:
            loglevel = logging.WARNING
        elif pargs.verbose == 1:
            loglevel = logging.INFO
        elif pargs.verbose > 1:
            loglevel = logging.DEBUG
        logger.setLevel(loglevel)
