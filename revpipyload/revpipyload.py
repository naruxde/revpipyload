#!/usr/bin/python3
#
# RevPiPyLoad
# Version: 0.2.0
#
# Webpage: https://revpimodio.org/
# (c) Sven Sager, License: LGPLv3
#
# -*- coding: utf-8 -*-
import proginit
import shlex
import signal
import subprocess
from concurrent import futures
from threading import Thread, Event
from time import sleep, asctime
from xmlrpc.server import SimpleXMLRPCServer


class RevPiPlc(Thread):

    def __init__(self, pargs, logger, lst_proc):
        super().__init__()
        self.autoreload = False
        self._evt_exit = Event()
        self.exitcode = 0
        self._lst_proc = lst_proc
        #self._lst_proc = ["ls", "/"]
        self._logger = logger
        self._pargs = pargs
        self._procplc = None
        self.zeroonexit = False

    def run(self):
        # Prozess starten
        self._logger.info("start plc program")
        if self._pargs.daemon:
            fh = open("/var/log/revpipyloadapp", "a")
            fh.write("started {}\n".format(asctime()))
            fh.flush()
        elif self.pargs.logfile is not None:
            fh = open(self.pargs.logfile, "a")
            fh.write("started {}\n".format(asctime()))
            fh.flush()
        else:
            fh = None

        # Prozess erstellen
        self._procplc = subprocess.Popen(self._lst_proc, bufsize=1, stdout=fh, stderr=subprocess.STDOUT)

        while not self._evt_exit.is_set():

            # Auswerten
            self.exitcode = self._procplc.poll()

            if self.exitcode is not None:
                if self.exitcode > 0:
                    self._logger.error(
                        "plc program chrashed - exitcode: {}".format(
                            self.exitcode
                        )
                    )
                    if self.zeroonexit:
                        f = open("/dev/piControl0", "w+b", 0)
                        f.write(bytes(4096))
                        self._logger.warning("set piControl0 to ZERO")
                else:
                    self._logger.info("plc program did a clean exit")

                if not self._evt_exit.is_set() and self.autoreload:
                    # Prozess neu starten
                    self._procplc = subprocess.Popen(self._lst_proc, bufsize=1, stdout=fh, stderr=subprocess.STDOUT)
                    if self.exitcode == 0:
                        self._logger.warning(
                            "restart plc program after clean exit"
                        )
                    else:
                        self._logger.warning("restart plc program after crash")
                else:
                    break

            self._evt_exit.wait(1)

        # Prozess beenden
        count = 0
        self._logger.info("term plc program")
        self._procplc.terminate()
        while self._procplc.poll() is None and count < 10:
            count += 1
            self._logger.debug(
                "wait term plc program {} seconds".format(count * 0.5)
            )
            sleep(0.5)
        if self._procplc.poll() is None:
            self._logger.warning("can not term plc program")
            self._procplc.kill()
            self._logger.warning("killed plc program")

        self.exitcode = self._procplc.poll()

    def stop(self):
        self._evt_exit.set()


class RevPiPyLoad(proginit.ProgInit):

    def __init__(self):
        super().__init__()
        self._exit = True
        self.evt_loadconfig = Event()

        self.autoreload = None
        self.plc = None
        self.plcprog = None
        self.plcslave = None
        self.tpe = None
        self.xmlrpc = None
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
            self.logger.info(
                "shutdown python plc program while getting new config"
            )
            self.stop()
            pauseproc = True

        # Konfigurationsdatei laden
        self.logger.info(
            "loading config file: {}".format(self.globalconffile)
        )
        self.globalconfig.read(self.globalconffile)

        # Konfiguration verarbeiten
        self.autoreload = int(self.globalconfig["DEFAULT"].get("autoreload", 1))
        self.autostart = int(self.globalconfig["DEFAULT"].get("autostart", 0))
        self.plcprog = self.globalconfig["DEFAULT"].get("plcprogram", None)
        self.plcslave = int(self.globalconfig["DEFAULT"].get("plcslave", 0))
        self.xmlrpc = int(self.globalconfig["DEFAULT"].get("xmlrpc", 1))
        self.zeroonexit = int(self.globalconfig["DEFAULT"].get("zeroonexit", 1))

        # PLC Thread konfigurieren
        self.logger.debug("create PLC watcher")
        self.plc = RevPiPlc(
            self.pargs,
            self.logger,
            shlex.split("/usr/bin/env python3 -u " + self.plcprog)
        )
        self.plc.autoreload = self.autoreload
        self.plc.zeroonexit = self.zeroonexit
        self.logger.debug("created PLC watcher")

        # XMLRPC-Server Instantiieren und konfigurieren
        if self.xmlrpc:
            self.logger.debug("create xmlrpc server")
            self.xsrv = SimpleXMLRPCServer(
                (
                    "",
                    int(self.globalconfig["DEFAULT"].get("xmlrpcport", 55123))
                ),
                logRequests=False,
                allow_none=True
            )
            self.xsrv.register_introspection_functions()

            self.xsrv.register_function(self.xml_getapplog, "get_applog")
            self.xsrv.register_function(self.xml_getplclog, "get_plclog")
            self.xsrv.register_function(self.xml_plcexitcode, "plcexitcode")
            self.xsrv.register_function(self.xml_plcrestart, "plcrestart")
            self.xsrv.register_function(self.xml_plcrunning, "plcrunning")
            self.xsrv.register_function(self.xml_plcstart, "plcstart")
            self.xsrv.register_function(self.xml_plcstop, "plcstop")
            self.xsrv.register_function(self.xml_reload, "reload")
            self.logger.debug("created xmlrpc server")

        if pauseproc:
            self.logger.info(
                "start python plc program after getting new config"
            )
            self.start()

    def _sigexit(self, signum, frame):
        """Signal handler to clean an exit program."""
        self.logger.info("got exit signal")
        self.stop()

    def _sigloadconfig(self, signum, frame):
        self.logger.info("got reload config signal")
        self.evt_loadconfig.set()

    def start(self):
        """Start python program and watching it."""
        self.logger.info("starting revpipyload")
        self._exit = False

        if self.xmlrpc:
            self.logger.info("start xmlrpc-server")
            self.tpe = futures.ThreadPoolExecutor(max_workers=1)
            self.tpe.submit(self.xsrv.serve_forever)

        if self.autostart:
            self.logger.info("starting plc program {}".format(self.plcprog))
            self.plc.start()

        while not self._exit \
                and not self.evt_loadconfig.is_set():
            self.evt_loadconfig.wait(1)

        if not self._exit:
            self.logger.info("exit python plc program to reload config")
            self._loadconfig()

    def stop(self):
        """Stop python program."""
        self.logger.info("stopping revpipyload")
        self._exit = True

        self.logger.info("stopping plc program {}".format(self.plcprog))
        self.plc.stop()
        self.plc.join()

        if self.xmlrpc:
            self.logger.info("shutting down xmlrpc-server")
            self.xsrv.shutdown()
            self.tpe.shutdown()
            self.xsrv.server_close()

    def xml_getapplog(self):
        self.logger.debug("xmlrpc call getapplog")
        fh = open("/var/log/revpipyloadapp")
        return fh.read()

    def xml_getplclog(self):
        self.logger.debug("xmlrpc call getplclog")
        fh = open("/var/log/revpipyload")
        return fh.read()

    def xml_plcexitcode(self):
        self.logger.debug("xmlrpc call plcexitcode")
        return -1 if self.plc.is_alive() else self.plc.exitcode

    def xml_plcrestart(self):
        self.logger.debug("xmlrpc call plcrestart")
        self.plc.stop()
        self.plc.join()
        exitcode = self.plc.exitcode
        self.plc = RevPiPlc(
            self.pargs,
            self.logger,
            shlex.split("/usr/bin/env python3 -u" + self.plcprog)
        )
        self.plc.autoreload = self.autoreload
        self.plc.zeroonexit = self.zeroonexit
        self.plc.start()
        return (exitcode, self.plc.exitcode)

    def xml_plcrunning(self):
        self.logger.debug("xmlrpc call plcrunning")
        return self.plc.is_alive()

    def xml_plcstart(self):
        self.logger.debug("xmlrpc call plcstart")
        if self.plc.is_alive():
            return -1
        else:
            self.plc = RevPiPlc(
                self.pargs,
                self.logger,
                shlex.split("/usr/bin/env python3 -u" + self.plcprog)
            )
            self.plc.autoreload = self.autoreload
            self.plc.zeroonexit = self.zeroonexit
            self.plc.start()
            return self.plc.exitcode

    def xml_plcstop(self):
        self.logger.debug("xmlrpc call plcstop")
        self.plc.stop()
        self.plc.join()
        return self.plc.exitcode

    def xml_reload(self):
        self.logger.debug("xmlrpc call reload")
        self.evt_loadconfig.set()


if __name__ == "__main__":
    root = RevPiPyLoad()
    root.start()
