#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
# -*- coding: utf-8 -*-
"""Main functions of our program."""
import logging
import os.path
import sys
from argparse import ArgumentParser
from os import fork as osfork


forked = False
globalconffile = None
logapp = "revpipyloadapp.log"
logplc = "revpipyload.log"
logger = None
pargs = None
startdir = None


def cleanup():
    """Clean up program."""
    # Logging beenden
    logging.shutdown()


def configure():
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
        "--procimg", dest="procimg",
        help="Path to process image"
    )
    parser.add_argument(
        "--pictory", dest="configrsc",
        help="piCtory file to use"
    )
    parser.add_argument(
        "-v", "--verbose", action="count", dest="verbose",
        help="Switch on verbose logging"
    )
    global pargs
    pargs = parser.parse_args()

    # Pfade absolut umschreiben
    global startdir
    if startdir is None:
        startdir = os.path.abspath(".")
    if pargs.conffile is not None and os.path.dirname(pargs.conffile) == "":
        pargs.conffile = os.path.join(startdir, pargs.conffile)
    if pargs.logfile is not None and os.path.dirname(pargs.logfile) == "":
        pargs.logfile = os.path.join(startdir, pargs.logfile)

    # Prüfen ob als Daemon ausgeführt werden soll
    global forked
    pidfile = "/var/run/revpipyload.pid"
    pid = 0
    if pargs.daemon and not forked:
        # Prüfen ob daemon schon läuft
        if os.path.exists(pidfile):
            raise SystemError(
                "program already running as daemon. check {}".format(pidfile)
            )

        # Zum daemon machen
        pid = osfork()
        if pid > 0:
            with open(pidfile, "w") as f:
                f.write(str(pid))
            sys.exit(0)
        else:
            forked = True

    global logapp
    global logplc
    if pargs.daemon:
        # Ausgaben umhängen in Logfile
        logapp = "/var/log/revpipyloadapp"
        logplc = "/var/log/revpipyload"
        pargs.conffile = "/etc/revpipyload/revpipyload.conf"
        sys.stdout = open(logplc, "a")
        sys.stderr = sys.stdout
    elif pargs.logfile is not None:
        logplc = pargs.logfile

    # Initialize configparser globalconfig
    global globalconffile
    globalconffile = pargs.conffile

    # Program logger
    global logger
    if logger is None:
        logger = logging.getLogger()

    # Alle handler entfernen
    for lhandler in logger.handlers:
        logger.removeHandler(lhandler)

    # Neue Handler bauen
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
