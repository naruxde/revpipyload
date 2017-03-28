#!/usr/bin/python3
#
# RevPiPyLoad
# Version: see global var pyloadverion
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
# -*- coding: utf-8 -*-
"""Revolution Pi Python PLC Loader.

Stellt das RevPiPyLoad Programm bereit. Dieses Programm lauft als Daemon auf
dem Revolution Pi. Es stellt Funktionen bereit, die es ermoeglichen ein Python
Programm zu starten und fuehrt dessen Ausgaben in eine Logdatei. Die Logdaten
koennen am Pi selber oder ueber eine XML-RPC Schnittstelle ausgelesen werden.

Dieser Daemon prueft ob das Python Programm noch lauft und kann es im Fall
eines Absturzes neu starten.

Ueber diesen Daemon kann die gesamte piCtory Konfiguration exportiert oder
importiert, ein Dump vom Prozessabbild gezogen und das eigene Python
Programm hochgeladen werden.

Es kann von dem Python Programm auch eine Archivdatei herunterladen werden,
welche optional auch die piCtory Konfiguraiton beinhaltet. Damit kann man sehr
schnell auf einem Revolution Pi das Programm inkl. piCtory Konfiguration
austauschen.

Die Zugriffsmoeglichkeiten koennen ueber einen Konfigurationsparameter
begrenzt werden!

"""
import gzip
import proginit
import os
import shlex
import signal
import subprocess
import tarfile
import zipfile
from concurrent import futures
from configparser import ConfigParser
from json import loads as jloads
from re import match as rematch
from shutil import rmtree
from sys import stdout as sysstdout
from tempfile import mktemp
from threading import Thread, Event, Lock
from time import sleep, asctime
from xmlrpc.client import Binary
from xmlrpc.server import SimpleXMLRPCServer

configrsc = "/opt/KUNBUS/config.rsc"
picontrolreset = "/opt/KUNBUS/piControlReset"
procimg = "/dev/piControl0"
pyloadverion = "0.2.11"


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

    def closeall(self):
        """Fuehrt close auf File Handler durch."""
        if self.fhapp is not None:
            self.fhapp.close()
        if self.fhplc is not None:
            self.fhplc.close()

    def get_applines(self):
        """Gibt neue Zeilen ab letzen Aufruf zurueck.
        @returns: list() mit neuen Zeilen"""
        if not os.access(proginit.logapp, os.R_OK):
            return []
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

            proginit.logger.debug(
                "got {} new app log lines".format(len(lst_new))
            )
            return lst_new

    def get_applog(self):
        """Gibt die gesamte Logdatei zurueck.
        @returns: str() mit Logdaten"""
        if not os.access(proginit.logapp, os.R_OK):
            proginit.logger.error(
                "can not access logfile {}".format(proginit.logapp)
            )
            return ""
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
            return []
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

            proginit.logger.debug(
                "got {} new pyloader log lines".format(len(lst_new))
            )
            return lst_new

    def get_plclog(self):
        """Gibt die gesamte Logdatei zurueck.
        @returns: str() mit Logdaten"""
        if not os.access(proginit.logplc, os.R_OK):
            proginit.logger.error(
                "can not access logfile {}".format(proginit.logplc)
            )
            return ""
        else:
            if self.fhplc is None or self.fhplc.closed:
                self.fhplc = open(proginit.logplc)
            self.fhplc.seek(0)
            return self.fhplc.read()


class PipeLogwriter(Thread):

    """File PIPE fuer das Schreiben des APP Log.

    Spezieller LogFile-Handler fuer die Ausgabe des subprocess fuer das Python
    PLC Programm. Die Ausgabe kann nicht auf einen neuen FileHandler
    umgeschrieben werden. Dadurch waere es nicht moeglich nach einem logrotate
    die neue Datei zu verwenden. Ueber die PIPE wird dies umgangen.

    """

    def __init__(self, logfilename):
        """Instantiiert PipeLogwriter-Klasse.
        @param logfilename: Dateiname fuer Logdatei"""
        super().__init__()
        self._exit = Event()
        self._fh = None
        self._lckfh = Lock()
        self.logfile = logfilename

        # Logdatei öffnen
        self._fh = self._configurefh()

        # Pipes öffnen
        self._fdr, self.fdw = os.pipe()
        proginit.logger.debug("pipe fd read: {} / write: {}".format(
            self._fdr, self.fdw
        ))

    def __del__(self):
        if self._fh is not None:
            self._fh.close()

    def _configurefh(self):
        """Konfiguriert den FileHandler fuer Ausgaben der PLCAPP.
        @returns: FileHandler-Objekt"""
        proginit.logger.debug("enter PipeLogwriter._configurefh()")

        dirname = os.path.dirname(self.logfile)

        proginit.logger.debug("dirname = {}".format(os.path.abspath(dirname)))

        if os.access(dirname, os.R_OK | os.W_OK):
            logfile = open(self.logfile, "a")
        else:
            raise RuntimeError("can not open logfile {}".format(self.logfile))

        proginit.logger.debug("leave PipeLogwriter._configurefh()")
        return logfile

    def logline(self, message):
        """Schreibt eine Zeile in die Logdatei oder stdout.
        @param message: Logzeile zum Schreiben"""
        with self._lckfh:
            self._fh.write("{}\n".format(message))
            self._fh.flush()

    def newlogfile(self):
        """Konfiguriert den FileHandler auf eine neue Logdatei."""
        proginit.logger.debug("enter RevPiPlc.newlogfile()")
        with self._lckfh:
            self._fh.close()
            self._fh = self._configurefh()
        proginit.logger.debug("leave RevPiPlc.newlogfile()")

    def run(self):
        """Prueft auf neue Logzeilen und schreibt diese."""
        proginit.logger.debug("enter PipeLogwriter.run()")

        fhread = os.fdopen(self._fdr)
        proginit.logger.debug("enter logreader pipe loop")
        while not self._exit.is_set():
            line = fhread.readline()
            self._lckfh.acquire()
            try:
                self._fh.write(line)
                self._fh.flush()
            except:
                proginit.logger.exception("PipeLogwriter write log line")
            finally:
                self._lckfh.release()
        proginit.logger.debug("leave logreader pipe loop")

        proginit.logger.debug("close all pipes")
        os.close(self._fdr)
        os.close(self.fdw)
        proginit.logger.debug("closed all pipes")

        proginit.logger.debug("leave PipeLogwriter.run()")

    def stop(self):
        """Beendetden Thread und die FileHandler werden geschlossen."""
        proginit.logger.debug("enter PipeLogwriter.stop()")
        self._exit.set()
        self._lckfh.acquire()
        try:
            os.write(self.fdw, b"\n")
        except:
            pass
        finally:
            self._lckfh.release()

        proginit.logger.debug("leave PipeLogwriter.stop()")


class RevPiPlc(Thread):

    """Verwaltet das PLC Python Programm.

    Dieser Thread startet das PLC Python Programm und ueberwacht es. Sollte es
    abstuerzen kann es automatisch neu gestartet werden. Die Ausgaben des
    Programms werden in eine Logdatei umgeleitet, damit der Entwickler sein
    Programm analysieren und debuggen kann.

    """

    def __init__(self, program, arguments, pversion):
        """Instantiiert RevPiPlc-Klasse."""
        super().__init__()
        self.autoreload = False
        self._arguments = arguments
        self._evt_exit = Event()
        self.exitcode = None
        self.gid = 65534
        self._plw = self._configureplw()
        self._program = program
        self._procplc = None
        self._pversion = pversion
        self.uid = 65534
        self.zeroonerror = False
        self.zeroonexit = False

    def _configureplw(self):
        """Konfiguriert den PipeLogwriter fuer Ausgaben der PLCAPP.
        @returns: PipeLogwriter()"""
        proginit.logger.debug("enter RevPiPlc._configureplw()")
        logfile = None
        if proginit.pargs.daemon:
            if os.access(os.path.dirname(proginit.logapp), os.R_OK | os.W_OK):
                logfile = proginit.logapp
        elif proginit.pargs.logfile is not None:
            logfile = proginit.pargs.logfile

        if not logfile is None:
            logfile = PipeLogwriter(logfile)

        proginit.logger.debug("leave RevPiPlc._configureplw()")
        return logfile

    def _setuppopen(self):
        """Setzt UID und GID fuer das PLC Programm."""
        proginit.logger.debug(
            "set uid {} and gid {}".format(self.uid, self.gid))
        os.setgid(self.gid)
        os.setuid(self.uid)

    def _spopen(self, lst_proc):
        """Startet das PLC Programm.
        @param lst_proc: Prozessliste
        @returns: subprocess"""
        proginit.logger.debug("enter RevPiPlc._spopen({})".format(lst_proc))
        sp = subprocess.Popen(
            lst_proc,
            preexec_fn=self._setuppopen,
            cwd=os.path.dirname(self._program),
            bufsize=1,
            stdout=sysstdout if self._plw is None else self._plw.fdw,
            stderr=subprocess.STDOUT
        )
        proginit.logger.debug("leave RevPiPlc._spopen()")
        return sp

    def _zeroprocimg(self):
        """Setzt Prozessabbild auf NULL."""
        if os.path.exists("/dev/piControl0"):
            f = open("/dev/piControl0", "w+b", 0)
            f.write(bytes(4096))

    def newlogfile(self):
        """Konfiguriert die FileHandler auf neue Logdatei."""
        proginit.logger.debug("enter RevPiPlc.newlogfile()")
        if self._plw is not None:
            self._plw.newlogfile()
            self._plw.logline("-" * 55)
            self._plw.logline("start new logfile: {}".format(asctime()))

        proginit.logger.debug("leave RevPiPlc.newlogfile()")

    def run(self):
        """Fuehrt PLC-Programm aus und ueberwacht es."""
        proginit.logger.debug("enter RevPiPlc.run()")
        if self._pversion == 2:
            lst_proc = shlex.split("/usr/bin/env python2 -u {} {}".format(
                self._program, self._arguments
            ))
        else:
            lst_proc = shlex.split("/usr/bin/env python3 -u {} {}".format(
                self._program, self._arguments
            ))

        # Prozess erstellen
        proginit.logger.info("start plc program {}".format(self._program))
        self._procplc = self._spopen(lst_proc)

        # LogWriter starten und Logausgaben schreiben
        if self._plw is not None:
            self._plw.logline("-" * 55)
            self._plw.logline("plc: {} started: {}".format(
                os.path.basename(self._program), asctime()
            ))
            self._plw.start()

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
                        proginit.logger.warning(
                            "set piControl0 to ZERO after PLC program error")

                else:
                    # PLC Python Programm sauber beendet
                    proginit.logger.info("plc program did a clean exit")
                    if self.zeroonexit:
                        self._zeroprocimg()
                        proginit.logger.info(
                            "set piControl0 to ZERO after PLC program returns "
                            "clean exitcode")

                if not self._evt_exit.is_set() and self.autoreload:
                    # Prozess neu starten
                    self._procplc = self._spopen(lst_proc)
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

        proginit.logger.debug("leave RevPiPlc.run()")

    def stop(self):
        """Beendet PLC-Programm."""
        proginit.logger.debug("enter RevPiPlc.stop()")
        proginit.logger.info("stop revpiplc thread")
        self._evt_exit.set()

        # Prüfen ob es einen subprocess gibt
        if self._procplc is None:
            if self._plw is not None:
                self._plw.stop()
                proginit.logger.debug("join after NONE pipe thread")
                self._plw.join()
                proginit.logger.debug("joined after NONE pipe thread")

            proginit.logger.debug("leave RevPiPlc.stop()")
            return

        # Prozess beenden
        count = 0
        proginit.logger.info("term plc program {}".format(self._program))
        self._procplc.terminate()

        while self._procplc.poll() is None and count < 10:
            count += 1
            proginit.logger.info(
                "wait term plc program {} seconds".format(count * 0.5)
            )
            sleep(0.5)
        if self._procplc.poll() is None:
            proginit.logger.warning(
                "can not term plc program {}".format(self._program)
            )
            self._procplc.kill()
            proginit.logger.warning("killed plc program")

        # Exitcode auswerten
        self.exitcode = self._procplc.poll()
        if self.zeroonexit and self.exitcode == 0 \
                or self.zeroonerror and self.exitcode != 0:
            self._zeroprocimg()

        if self._plw is not None:
            self._plw.stop()
            proginit.logger.debug("join pipe thread")
            self._plw.join()
            proginit.logger.debug("joined pipe thread")

        proginit.logger.debug("leave RevPiPlc.stop()")


class RevPiPyLoad():

    """Hauptklasse, die alle Funktionen zur Verfuegung stellt.

    Hier wird die gesamte Konfiguraiton eingelesen und der ggf. aktivierte
    XML-RPC-Server gestartet.

    """

    def __init__(self):
        """Instantiiert RevPiPyLoad-Klasse."""
        proginit.configure()

        self._exit = True
        self.evt_loadconfig = Event()
        self.globalconfig = ConfigParser()
        self.logr = LogReader()
        self.plc = None
        self.tfile = {}
        self.tpe = None
        self.xsrv = None

        # Load config
        self._loadconfig()

        # Signal events
        signal.signal(signal.SIGINT, self._sigexit)
        signal.signal(signal.SIGTERM, self._sigexit)
        signal.signal(signal.SIGHUP, self._sigloadconfig)
        signal.signal(signal.SIGUSR1, self._signewlogfile)

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
            "loading config file: {}".format(proginit.globalconffile)
        )
        self.globalconfig.read(proginit.globalconffile)

        # Konfiguration verarbeiten
        self.autoreload = \
            int(self.globalconfig["DEFAULT"].get("autoreload", 1))
        self.autostart = \
            int(self.globalconfig["DEFAULT"].get("autostart", 0))
        self.plcprog = \
            self.globalconfig["DEFAULT"].get("plcprogram", "none.py")
        self.plcarguments = \
            self.globalconfig["DEFAULT"].get("plcarguments", "")
        self.plcworkdir = \
            self.globalconfig["DEFAULT"].get("plcworkdir", ".")
        self.plcslave = \
            int(self.globalconfig["DEFAULT"].get("plcslave", 0))
        self.pythonver = \
            int(self.globalconfig["DEFAULT"].get("pythonversion", 3))
        self.xmlrpc = \
            int(self.globalconfig["DEFAULT"].get("xmlrpc", 0))
        self.zeroonerror = \
            int(self.globalconfig["DEFAULT"].get("zeroonerror", 1))
        self.zeroonexit = \
            int(self.globalconfig["DEFAULT"].get("zeroonexit", 1))

        # Workdirectory wechseln
        os.chdir(self.plcworkdir)

        # PLC Thread konfigurieren
        self.plc = self._plcthread()

        # XMLRPC-Server Instantiieren und konfigurieren
        if self.xmlrpc >= 1:
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

            # XML Modus 1 Nur Logs lesen und PLC Programm neu starten
            self.xsrv.register_function(self.logr.get_applines, "get_applines")
            self.xsrv.register_function(self.logr.get_applog, "get_applog")
            self.xsrv.register_function(self.logr.get_plclines, "get_plclines")
            self.xsrv.register_function(self.logr.get_plclog, "get_plclog")
            self.xsrv.register_function(self.xml_plcexitcode, "plcexitcode")
            self.xsrv.register_function(self.xml_plcrunning, "plcrunning")
            self.xsrv.register_function(self.xml_plcstart, "plcstart")
            self.xsrv.register_function(self.xml_plcstop, "plcstop")
            self.xsrv.register_function(self.xml_reload, "reload")

            # XML Modus 2 Einstellungen lesen und Programm herunterladen
            if self.xmlrpc >= 2:
                self.xsrv.register_function(
                    self.xml_getconfig, "get_config")
                self.xsrv.register_function(
                    self.xml_getfilelist, "get_filelist")
                self.xsrv.register_function(
                    self.xml_getpictoryrsc, "get_pictoryrsc")
                self.xsrv.register_function(
                    self.xml_getprocimg, "get_procimg")
                self.xsrv.register_function(
                    self.xml_plcdownload, "plcdownload")

            # XML Modus 3 Programm und Konfiguration hochladen
            if self.xmlrpc >= 3:
                self.xsrv.register_function(
                    self.xml_plcupload, "plcupload")
                self.xsrv.register_function(
                    self.xml_plcuploadclean, "plcuploadclean")
                self.xsrv.register_function(
                    self.xml_setconfig, "set_config")
                self.xsrv.register_function(
                    self.xml_setpictoryrsc, "set_pictoryrsc")

            self.xsrv.register_function(lambda: pyloadverion, "version")
            self.xsrv.register_function(lambda: self.xmlrpc, "xmlmodus")
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
        if not os.path.exists(os.path.join(self.plcworkdir, self.plcprog)):
            proginit.logger.error("plc file does not exists {}".format(
                os.path.join(self.plcworkdir, self.plcprog)
            ))
            return None

        proginit.logger.debug("create PLC watcher")
        th_plc = RevPiPlc(
            os.path.join(self.plcworkdir, self.plcprog),
            self.plcarguments,
            self.pythonver
        )
        th_plc.autoreload = self.autoreload
        th_plc.gid = int(self.globalconfig["DEFAULT"].get("plcgid", 65534))
        th_plc.uid = int(self.globalconfig["DEFAULT"].get("plcuid", 65534))
        th_plc.zeroonerror = self.zeroonerror
        th_plc.zeroonexit = self.zeroonexit
        proginit.logger.debug("created PLC watcher")
        return th_plc

    def _sigexit(self, signum, frame):
        """Signal handler to clean and exit program."""
        proginit.logger.debug("got exit signal")
        self.stop()

        # Programm aufräumen
        proginit.cleanup()

        proginit.logger.debug("end revpipyload program")

    def _sigloadconfig(self, signum, frame):
        """Signal handler to load configuration."""
        proginit.logger.debug("got reload config signal")
        self.evt_loadconfig.set()

    def _signewlogfile(self, signum, frame):
        """Signal handler to start new logfile."""
        proginit.logger.debug("got new logfile signal")

        # Logger neu konfigurieren
        proginit.configure()
        proginit.logger.info("start new logfile: {}".format(asctime()))

        # stdout für revpipyplc
        if self.plc is not None:
            self.plc.newlogfile()

        # Logreader schließen
        self.logr.closeall()

    def packapp(self, mode="tar", pictory=False):
        """Erzeugt aus dem PLC-Programm ein TAR-File.

        @param mode: Packart 'tar' oder 'zip'
        @param pictory: piCtory Konfiguration mit einpacken
        @returns: Dateinamen des Archivs

        """
        filename = mktemp(suffix=".packed", prefix="plc")

        if mode == "zip":
            fh_pack = zipfile.ZipFile(filename, mode="w")
            wd = os.walk("./")
            try:
                for tup_dir in wd:
                    for file in tup_dir[2]:
                        arcname = os.path.join(
                            os.path.basename(self.plcworkdir),
                            tup_dir[0][2:],
                            file
                        )
                        fh_pack.write(
                            os.path.join(tup_dir[0], file), arcname=arcname
                        )
                if pictory:
                    fh_pack.write(configrsc, arcname="config.rsc")
            except:
                filename = ""
            finally:
                fh_pack.close()

        else:
            fh_pack = tarfile.open(
                name=filename, mode="w:gz", dereference=True)
            try:
                fh_pack.add(".", arcname=os.path.basename(self.plcworkdir))
                if pictory:
                    fh_pack.add(configrsc, arcname="config.rsc")
            except:
                filename = ""
            finally:
                fh_pack.close()

        return filename

    def start(self):
        """Start plcload and PLC python program."""
        proginit.logger.info("starting revpipyload")
        self._exit = False

        if self.xmlrpc >= 1:
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

        if self.plc is not None and self.plc.is_alive():
            proginit.logger.debug("stopping revpiplc-thread")
            self.plc.stop()
            self.plc.join()

        if self.xmlrpc >= 1:
            proginit.logger.info("shutting down xmlrpc-server")
            self.xsrv.shutdown()
            self.tpe.shutdown()
            self.xsrv.server_close()

    def xml_getconfig(self):
        """Uebertraegt die RevPiPyLoad Konfiguration.
        @returns: dict() der Konfiguration"""
        proginit.logger.debug("xmlrpc call getconfig")
        dc = {}
        dc["autoreload"] = self.autoreload
        dc["autostart"] = self.autostart
        dc["plcworkdir"] = self.plcworkdir
        dc["plcprogram"] = self.plcprog
        dc["plcarguments"] = self.plcarguments
        dc["plcslave"] = self.plcslave
        dc["pythonversion"] = self.pythonver
        dc["xmlrpc"] = self.xmlrpc
        dc["xmlrpcport"] = \
            self.globalconfig["DEFAULT"].get("xmlrpcport", 55123)
        dc["zeroonerror"] = self.zeroonerror
        dc["zeroonexit"] = self.zeroonexit
        return dc

    def xml_getfilelist(self):
        """Uebertraegt die Dateiliste vom plcworkdir.
        @returns: list() mit Dateinamen"""
        proginit.logger.debug("xmlrpc call getfilelist")
        lst_file = []
        wd = os.walk("./")
        for tup_dir in wd:
            for file in tup_dir[2]:
                lst_file.append(os.path.join(tup_dir[0], file)[2:])
        return lst_file

    def xml_getpictoryrsc(self):
        """Gibt die config.rsc Datei von piCotry zurueck.
        @returns: xmlrpc.client.Binary()"""
        proginit.logger.debug("xmlrpc call getpictoryrsc")
        with open(configrsc, "rb") as fh:
            buff = fh.read()
        return Binary(buff)

    def xml_getprocimg(self):
        """Gibt die Rohdaten aus piControl0 zurueck.
        @returns: xmlrpc.client.Binary()"""
        proginit.logger.debug("xmlrpc call getprocimg")
        with open(procimg, "rb") as fh:
            buff = fh.read()
        return Binary(buff)

    def xml_plcdownload(self, mode="tar", pictory=False):
        """Uebertraegt ein Archiv vom plcworkdir.

        @param mode: Archivart 'tar' 'zip'
        @param pictory: piCtory Konfiguraiton mit einpacken
        @returns: Binary() mit Archivdatei

        """
        proginit.logger.debug("xmlrpc call plcdownload")

        # TODO: Daten blockweise übertragen

        file = self.packapp(mode, pictory)
        if os.path.exists(file):
            fh = open(file, "rb")
            xmldata = Binary(fh.read())
            fh.close()
            os.remove(file)
            return xmldata
        return Binary()

    def xml_plcexitcode(self):
        """Gibt den aktuellen exitcode vom PLC Programm zurueck.

        @returns: int() exitcode oder:
            -1 laeuft noch
            -2 Datei nicht gefunden
            -3 Lief nie

        """
        proginit.logger.debug("xmlrpc call plcexitcode")
        if self.plc is None:
            return -2
        elif self.plc.is_alive():
            return -1
        else:
            return -3 if self.plc.exitcode is None else self.plc.exitcode

    def xml_plcrunning(self):
        """Prueft ob das PLC Programm noch lauft.
        @returns: True, wenn das PLC Programm noch lauft"""
        proginit.logger.debug("xmlrpc call plcrunning")
        return False if self.plc is None else self.plc.is_alive()

    def xml_plcstart(self):
        """Startet das PLC Programm.

        @returns: int() Status:
            -1 Programm lauft noch
            -2 Datei nicht gefunden

        """
        proginit.logger.debug("xmlrpc call plcstart")
        if self.plc is not None and self.plc.is_alive():
            return -1
        else:
            self.plc = self._plcthread()
            if self.plc is None:
                return -2
            else:
                self.plc.start()
                return 0

    def xml_plcstop(self):
        """Stoppt das PLC Programm.

        @returns: int() Exitcode vom PLC Programm
            -1 PLC Programm lief nicht

        """
        proginit.logger.debug("xmlrpc call plcstop")
        if self.plc is not None and self.plc.is_alive():
            self.plc.stop()
            self.plc.join()
            return self.plc.exitcode
        else:
            return -1

    def xml_plcupload(self, filedata, filename):
        """Empfaengt Dateien fuer das PLC Programm.

        @param filedata: GZIP Binary data der datei
        @param filename: Name inkl. Unterverzeichnis der Datei
        @returns: Ture, wenn Datei erfolgreich gespeichert wurde

        """
        proginit.logger.debug("xmlrpc call plcupload")
        noerr = False

        if filedata is None or filename is None:
            return False

        # Absoluten Pfad prüfen
        dirname = os.path.join(self.plcworkdir, os.path.dirname(filename))
        if self.plcworkdir not in os.path.abspath(dirname):
            return False

        # Ordner erzeugen
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        # Datei erzeugen
        try:
            fh = open(filename, "wb")
            fh.write(gzip.decompress(filedata.data))
            noerr = True
        finally:
            fh.close()

        return noerr

    def xml_plcuploadclean(self):
        """Loescht das gesamte plcworkdir Verzeichnis.
        @returns: True, wenn erfolgreich"""
        proginit.logger.debug("xmlrpc call plcuploadclean")
        try:
            rmtree(".", ignore_errors=True)
        except:
            return False
        return True

    def xml_reload(self):
        """Startet RevPiPyLoad neu und verwendet neue Konfiguraiton."""
        proginit.logger.debug("xmlrpc call reload")
        self.evt_loadconfig.set()

    def xml_setconfig(self, dc, loadnow=False):
        """Empfaengt die RevPiPyLoad Konfiguration.
        @returns: True, wenn erfolgreich angewendet"""
        proginit.logger.debug("xmlrpc call setconfig")
        keys = {
            "autoreload": "[01]",
            "autostart": "[01]",
            "plcprogram": ".+",
            "plcarguments": ".*",
            "plcslave": "[01]",
            "pythonversion": "[23]",
            "xmlrpc": "[0-3]",
            "xmlrpcport": "[0-9]{,5}",
            "zeroonerror": "[01]",
            "zeroonexit": "[01]"
        }

        # Werte übernehmen
        for key in keys:
            if key in dc:
                if rematch(keys[key], str(dc[key])) is None:
                    proginit.logger.error(
                        "got wrong setting '{}' with value '{}'".format(
                            key, dc[key]
                        )
                    )
                    return False
                self.globalconfig.set("DEFAULT", key, str(dc[key]))

        # conf-Datei schreiben
        fh = open(proginit.globalconffile, "w")
        self.globalconfig.write(fh)
        proginit.logger.info(
            "got new config and wrote it to {}".format(proginit.globalconffile)
        )

        if loadnow:
            # RevPiPyLoad neu konfigurieren
            self.evt_loadconfig.set()

        return True

    def xml_setpictoryrsc(self, filebytes, reset=False):
        """Schreibt die config.rsc Datei von piCotry.

        @param filebytes: xmlrpc.client.Binary()-Objekt
        @param reset: Reset piControl Device
        @returns: Statuscode:
            0 Alles erfolgreich
            -1 Kann JSON-Datei nicht laden
            -2 piCtory Elemente in JSON-Datei nicht gefunden
            -3 Konnte Konfiguraiton nicht schreiben
            Positive Zahl ist exitcode von piControlReset

        """
        proginit.logger.debug("xmlrpc call setpictoryrsc")

        # Datei als JSON laden
        try:
            jconfigrsc = jloads(filebytes.data.decode())
        except:
            return -1

        # Elemente prüfen
        lst_check = ["Devices", "Summary", "App"]
        for chk in lst_check:
            if chk not in jconfigrsc:
                return -2

        try:
            with open(configrsc, "wb") as fh:
                fh.write(filebytes.data)
        except:
            return -3
        else:
            if reset:
                return os.system(picontrolreset)
            else:
                return 0


if __name__ == "__main__":
    root = RevPiPyLoad()
    root.start()
