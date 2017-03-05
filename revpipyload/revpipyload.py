#!/usr/bin/python3
#
# RevPiPyLoad
# Version: 0.2.2
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
# -*- coding: utf-8 -*-
import proginit
import os
import shlex
import signal
import subprocess
from concurrent import futures
from threading import Thread, Event
from time import sleep, asctime
from xmlrpc.server import SimpleXMLRPCServer


class LogReader():

    """Ermoeglicht den Zugriff auf die Logdateien.

    Beinhaltet Funktionen fuer den Abruf der gesamten Logdatei fuer das
    RevPiPyLoad-System und die Logdatei der PLC-Anwendung.
    Ausserdem koennen nur neue Zeilen abgerufen werden, um eine dynamische
    Logansicht zu ermoeglichen.

    """

    def __init__(self):
        """Instantiiert LogReader-Klasse."""
        self.fhapp = None
        self.posapp = 0
        self.fhplc = None
        self.posplc = 0

    def get_applines(self):
        """Gibt neue Zeilen ab letzen Aufruf zurueck.
        @returns: list() mit neuen Zeilen"""
        if not os.access(proginit.logapp, os.R_OK):
            proginit.logger.error(
                "can not access logfile {}".format(proginit.logapp)
            )
            return None
        else:
            if self.fhapp is None or self.fhapp.closed:
                self.fhapp = open(proginit.logapp)

            lst_new = []
            while True:
                self.posapp = self.fhapp.tell()
                line = self.fhapp.readline()
                if line:
                    lst_new.append(line)
                else:
                    self.fhapp.seek(self.posapp)
                    break

            proginit.debug("got {} new log lines".format(len(lst_new)))
            return lst_new

    def get_applog(self):
        """Gibt die gesamte Logdatei zurueck.
        @returns: str() mit Logdaten"""
        if not os.access(proginit.logapp, os.R_OK):
            proginit.logger.error(
                "can not access logfile {}".format(proginit.logapp)
            )
            return None
        else:
            if self.fhapp is None or self.fhapp.closed:
                self.fhapp = open(proginit.logapp)
            self.fhapp.seek(0)
            return self.fhapp.read()

    def get_plclines(self):
        """Gibt neue Zeilen ab letzen Aufruf zurueck.
        @returns: list() mit neuen Zeilen"""
        if not os.access(proginit.logplc, os.R_OK):
            proginit.logger.error(
                "can not access logfile {}".format(proginit.logplc)
            )
            return None
        else:
            if self.fhplc is None or self.fhplc.closed:
                self.fhplc = open(proginit.logplc)

            lst_new = []
            while True:
                self.posplc = self.fhplc.tell()
                line = self.fhplc.readline()
                if line:
                    lst_new.append(line)
                else:
                    self.fhplc.seek(self.posplc)
                    break

            proginit.debug("got {} new log lines".format(len(lst_new)))
            return lst_new

    def get_plclog(self):
        """Gibt die gesamte Logdatei zurueck.
        @returns: str() mit Logdaten"""
        if not os.access(proginit.logplc, os.R_OK):
            proginit.logger.error(
                "can not access logfile {}".format(proginit.logplc)
            )
            return None
        else:
            if self.fhplc is None or self.fhplc.closed:
                self.fhplc = open(proginit.logplc)
            self.fhplc.seek(0)
            return self.fhplc.read()


class RevPiPlc(Thread):

    def __init__(self, program):
        """Instantiiert RevPiPlc-Klasse."""
        super().__init__()
        self.autoreload = False
        self._evt_exit = Event()
        self.exitcode = None
        self._program = program
        self._procplc = None
        self.zeroonerror = False
        self.zeroonexit = False

    def _zeroprocimg(self):
        """Setzt Prozessabbild auf NULL."""
        if os.exists("/dev/piControl0"):
            f = open("/dev/piControl0", "w+b", 0)
            f.write(bytes(4096))
            proginit.logger.warning("set piControl0 to ZERO")

    def run(self):
        """Fuehrt PLC-Programm aus und ueberwacht es."""
        lst_proc = shlex.split("/usr/bin/env python3 -u " + self.program)

        # Ausgaben konfigurieren und ggf. umleiten
        fh = None
        if proginit.pargs.daemon:
            if os.access(os.path.dirname(proginit.logapp), os.R_OK | os.W_OK):
                fh = proginit.logapp
        elif proginit.pargs.logfile is not None:
            fh = proginit.pargs.logfile

        if fh is not None:
            fh = open(fh, "a")
            fh.write("-" * 40)
            fh.write("\nplc app started: {}\n".format(asctime()))
            fh.flush()

        # Prozess erstellen
        proginit.logger.info("start plc program {}".format(self.program))
        self._procplc = subprocess.Popen(
            lst_proc, bufsize=1, stdout=fh, stderr=subprocess.STDOUT
        )

        while not self._evt_exit.is_set():

            # Auswerten
            self.exitcode = self._procplc.poll()

            if self.exitcode is not None:

                if self.exitcode > 0:
                    # PLC Python Programm abgestürzt
                    proginit.logger.error(
                        "plc program chrashed - exitcode: {}".format(
                            self.exitcode
                        )
                    )
                    if self.zeroonerror:
                        self._zeroprocimg()

                else:
                    # PLC Python Programm sauber beendet
                    proginit.logger.info("plc program did a clean exit")
                    if self.zeroonexit:
                        self._zeroprocimg()

                if not self._evt_exit.is_set() and self.autoreload:
                    # Prozess neu starten
                    self._procplc = subprocess.Popen(
                        lst_proc, bufsize=1, stdout=fh,
                        stderr=subprocess.STDOUT
                    )
                    if self.exitcode == 0:
                        proginit.logger.warning(
                            "restart plc program after clean exit"
                        )
                    else:
                        proginit.logger.warning(
                            "restart plc program after crash"
                        )
                else:
                    break

            self._evt_exit.wait(1)

        # Prozess beenden
        count = 0
        proginit.logger.info("term plc program {}".format(self.program))
        self._procplc.terminate()
        while self._procplc.poll() is None and count < 10:
            count += 1
            proginit.logger.info(
                "wait term plc program {} seconds".format(count * 0.5)
            )
            sleep(0.5)
        if self._procplc.poll() is None:
            proginit.logger.warning(
                "can not term plc program {}".format(self.program)
            )
            self._procplc.kill()
            proginit.logger.warning("killed plc program")

        # Exitcode auswerten
        self.exitcode = self._procplc.poll()
        if self.zeroonexit and self.exitcode == 0 \
                or self.zeroonerror and self.exitcode > 0:
            self._zeroprocimg()

    def stop(self):
        """Beendet PLC-Programm."""
        self._evt_exit.set()


class RevPiPyLoad(proginit.ProgInit):

    def __init__(self):
        """Instantiiert RevPiPyLoad-Klasse."""
        super().__init__()
        self._exit = True
        self.evt_loadconfig = Event()

        self.logr = LogReader()
        self.plc = None
        self.tpe = None
        self.xsrv = None

        # Load config
        self._loadconfig()

        # Signal events
        signal.signal(signal.SIGINT, self._sigexit)
        signal.signal(signal.SIGTERM, self._sigexit)
        signal.signal(signal.SIGHUP, self._sigloadconfig)

    def _loadconfig(self):
        """Load configuration file and setup modul."""
        self.evt_loadconfig.clear()
        pauseproc = False

        if not self._exit:
            proginit.logger.info(
                "shutdown revpipyload while getting new config"
            )
            self.stop()
            pauseproc = True

        # Konfigurationsdatei laden
        proginit.logger.info(
            "loading config file: {}".format(self.globalconffile)
        )
        self.globalconfig.read(self.globalconffile)

        # Konfiguration verarbeiten
        self.autoreload = int(self.globalconfig["DEFAULT"].get("autoreload", 1))
        self.autostart = int(self.globalconfig["DEFAULT"].get("autostart", 0))
        self.plcprog = self.globalconfig["DEFAULT"].get("plcprogram", None)
        self.plcworkdir = self.globalconfig["DEFAULT"].get(
            "plcworkdir", "/var/lib/revpipyload"
        )
        self.plcslave = int(self.globalconfig["DEFAULT"].get("plcslave", 0))
        self.xmlrpc = int(self.globalconfig["DEFAULT"].get("xmlrpc", 1))
        self.zerooneerror = int(
            self.globalconfig["DEFAULT"].get("zeroonerror", 1)
        )
        self.zeroonexit = int(
            self.globalconfig["DEFAULT"].get("zeroonexit", 1)
        )

        # PLC Thread konfigurieren
        self.plc = self._plcthread()

        # XMLRPC-Server Instantiieren und konfigurieren
        if self.xmlrpc:
            proginit.logger.debug("create xmlrpc server")
            self.xsrv = SimpleXMLRPCServer(
                (
                    "",
                    int(self.globalconfig["DEFAULT"].get("xmlrpcport", 55123))
                ),
                logRequests=False,
                allow_none=True
            )
            self.xsrv.register_introspection_functions()

            self.xsrv.register_function(self.logr.get_applines, "get_applines")
            self.xsrv.register_function(self.logr.get_applog, "get_applog")
            self.xsrv.register_function(self.logr.get_plclines, "get_plclines")
            self.xsrv.register_function(self.logr.get_plclog, "get_plclog")
            self.xsrv.register_function(self.xml_plcdownload, "plcdownload")
            self.xsrv.register_function(self.xml_plcexitcode, "plcexitcode")
            self.xsrv.register_function(self.xml_plcrunning, "plcrunning")
            self.xsrv.register_function(self.xml_plcstart, "plcstart")
            self.xsrv.register_function(self.xml_plcstop, "plcstop")
            self.xsrv.register_function(self.xml_plcupload, "plcupload")
            self.xsrv.register_function(self.xml_reload, "reload")
            proginit.logger.debug("created xmlrpc server")

        if pauseproc:
            proginit.logger.info(
                "start revpipyload after getting new config"
            )
            self.start()

    def _plcthread(self):
        """Konfiguriert den PLC-Thread fuer die Ausfuehrung.
        @returns: PLC-Thread Object or None"""

        # Prüfen ob Programm existiert
        if not os.exists(os.path.join(self.plcworkdir, self.plcprog)):
            proginit.logger.error("plc file does not exists {}")
            return

        proginit.logger.debug("create PLC watcher")
        th_plc = RevPiPlc(os.path.join(self.plcworkdir, self.plcprog))
        th_plc.autoreload = self.autoreload
        th_plc.zeroonerror = self.zerooneerror
        th_plc.zeroonexit = self.zeroonexit
        proginit.logger.debug("created PLC watcher")
        return th_plc

    def _sigexit(self, signum, frame):
        """Signal handler to clean an exit program."""
        proginit.logger.debug("got exit signal")
        self.stop()

    def _sigloadconfig(self, signum, frame):
        """Signal handler to load configuration."""
        proginit.logger.debug("got reload config signal")
        self.evt_loadconfig.set()

    def start(self):
        """Start plcload and PLC python program."""
        proginit.logger.info("starting revpipyload")
        self._exit = False

        if self.xmlrpc:
            proginit.logger.info("start xmlrpc-server")
            self.tpe = futures.ThreadPoolExecutor(max_workers=1)
            self.tpe.submit(self.xsrv.serve_forever)

        if self.autostart:
            proginit.logger.debug("starting revpiplc-thread")
            if self.plc is not None:
                self.plc.start()

        while not self._exit \
                and not self.evt_loadconfig.is_set():
            self.evt_loadconfig.wait(1)

        if not self._exit:
            proginit.logger.info("exit python plc program to reload config")
            self._loadconfig()

    def stop(self):
        """Stop PLC python program and plcload."""
        proginit.logger.info("stopping revpipyload")
        self._exit = True

        proginit.logger.debug("stopping revpiplc-thread")
        self.plc.stop()
        self.plc.join()

        if self.xmlrpc:
            proginit.logger.info("shutting down xmlrpc-server")
            self.xsrv.shutdown()
            self.tpe.shutdown()
            self.xsrv.server_close()

    def xml_plcdownload(self):
        pass

    def xml_plcexitcode(self):
        proginit.logger.debug("xmlrpc call plcexitcode")
        return -1 if self.plc.is_alive() else self.plc.exitcode

    def xml_plcrunning(self):
        proginit.logger.debug("xmlrpc call plcrunning")
        return self.plc.is_alive()

    def xml_plcstart(self):
        proginit.logger.debug("xmlrpc call plcstart")
        if self.plc.is_alive():
            return -1
        else:
            self.plc = self._plcthread()
            if self.plc is None:
                return 100
            else:
                self.plc.start()
                return 0

    def xml_plcstop(self):
        proginit.logger.debug("xmlrpc call plcstop")
        self.plc.stop()
        self.plc.join()
        return self.plc.exitcode

    def xml_plcupload(self, path=None, file=None, clear=False):
        pass

    def xml_reload(self):
        proginit.logger.debug("xmlrpc call reload")
        self.evt_loadconfig.set()


if __name__ == "__main__":
    root = RevPiPyLoad()
    root.start()
