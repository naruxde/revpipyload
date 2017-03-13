#!/usr/bin/python3
#
# RevPiPyLoad
# Version: see global var pyloadverion
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
import tarfile
import zipfile
from concurrent import futures
from shutil import rmtree
from tempfile import mktemp
from threading import Thread, Event
from time import sleep, asctime
from xmlrpc.client import Binary
from xmlrpc.server import SimpleXMLRPCServer

configrsc = "/opt/KUNBUS/config.rsc"
picontrolreset = "/opt/KUNBUS/piControlReset"
procimg = "/dev/piControl0"
pyloadverion = "0.2.3"


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


class RevPiPlc(Thread):

    def __init__(self, program, pversion):
        """Instantiiert RevPiPlc-Klasse."""
        super().__init__()
        self.autoreload = False
        self._evt_exit = Event()
        self.exitcode = None
        self._program = program
        self._procplc = None
        self._pversion = pversion
        self.zeroonerror = False
        self.zeroonexit = False

    def _zeroprocimg(self):
        """Setzt Prozessabbild auf NULL."""
        if os.path.exists("/dev/piControl0"):
            f = open("/dev/piControl0", "w+b", 0)
            f.write(bytes(4096))

    def run(self):
        """Fuehrt PLC-Programm aus und ueberwacht es."""
        if self._pversion == 2:
            lst_proc = shlex.split("/usr/bin/env python2 -u " + self._program)
        else:
            lst_proc = shlex.split("/usr/bin/env python3 -u " + self._program)

        # Ausgaben konfigurieren und ggf. umleiten
        fh = None
        if proginit.pargs.daemon:
            if os.access(os.path.dirname(proginit.logapp), os.R_OK | os.W_OK):
                fh = proginit.logapp
        elif proginit.pargs.logfile is not None:
            fh = proginit.pargs.logfile

        if fh is not None:
            fh = open(fh, "a")
            fh.write("-" * 45)
            fh.write("\nplc app started: {}\n".format(asctime()))
            fh.flush()

        # Prozess erstellen
        proginit.logger.info("start plc program {}".format(self._program))
        self._procplc = subprocess.Popen(
            lst_proc, cwd=os.path.dirname(self._program),
            bufsize=1, stdout=fh, stderr=subprocess.STDOUT
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
                    self._procplc = subprocess.Popen(
                        lst_proc, cwd=os.path.dirname(self._program),
                        bufsize=1, stdout=fh, stderr=subprocess.STDOUT
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
        proginit.logger.info("term plc program {}".format(self._program))

        # TODO: Prüfen ob es überhautp noch läuft

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
        self.tfile = {}
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
        self.autoreload = \
            int(self.globalconfig["DEFAULT"].get("autoreload", 1))
        self.autostart = \
            int(self.globalconfig["DEFAULT"].get("autostart", 0))
        self.plcprog = \
            self.globalconfig["DEFAULT"].get("plcprogram", None)
        self.plcworkdir = \
            self.globalconfig["DEFAULT"].get("plcworkdir","/var/lib/revpipyload")
        self.plcslave = \
            int(self.globalconfig["DEFAULT"].get("plcslave", 0))
        self.pythonver = \
            int(self.globalconfig["DEFAULT"].get("pythonversion", 3))
        self.xmlrpc = \
            int(self.globalconfig["DEFAULT"].get("xmlrpc", 1))
        self.zerooneerror = \
            int(self.globalconfig["DEFAULT"].get("zeroonerror", 1))
        self.zeroonexit = \
            int(self.globalconfig["DEFAULT"].get("zeroonexit", 1))

        # Workdirectory wechseln
        os.chdir(self.plcworkdir)

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

            self.xsrv.register_function(self.xml_getconfig, "get_config")
            self.xsrv.register_function(self.xml_getfilelist, "get_filelist")
            self.xsrv.register_function(self.xml_getpictoryrsc, "get_pictoryrsc")
            self.xsrv.register_function(self.xml_getprocimg, "get_procimg")
            self.xsrv.register_function(self.xml_plcdownload, "plcdownload")
            self.xsrv.register_function(self.xml_plcexitcode, "plcexitcode")
            self.xsrv.register_function(self.xml_plcrunning, "plcrunning")
            self.xsrv.register_function(self.xml_plcstart, "plcstart")
            self.xsrv.register_function(self.xml_plcstop, "plcstop")
            self.xsrv.register_function(self.xml_plcupload, "plcupload")
            self.xsrv.register_function(self.xml_plcuploadclean, "plcuploadclean")
            self.xsrv.register_function(self.xml_reload, "reload")
            self.xsrv.register_function(self.xml_setconfig, "set_config")
            self.xsrv.register_function(self.xml_setpictoryrsc, "set_pictoryrsc")

            self.xsrv.register_function(lambda: pyloadverion, "version")
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
            return

        proginit.logger.debug("create PLC watcher")
        th_plc = RevPiPlc(
            os.path.join(self.plcworkdir, self.plcprog), self.pythonver)
        th_plc.autoreload = self.autoreload
        th_plc.zeroonerror = self.zerooneerror
        th_plc.zeroonexit = self.zeroonexit
        proginit.logger.debug("created PLC watcher")
        return th_plc

    def _sigexit(self, signum, frame):
        """Signal handler to clean and exit program."""
        proginit.logger.debug("got exit signal")
        self.stop()

    def _sigloadconfig(self, signum, frame):
        """Signal handler to load configuration."""
        proginit.logger.debug("got reload config signal")
        self.evt_loadconfig.set()

    def packapp(self, mode="tar", pictory=False):
        """Erzeugt aus dem PLC-Programm ein TAR-File.
        @param mode: Packart 'tar' oder 'zip'
        @param pictory: piCtory Konfiguration mit einpacken"""
        filename = mktemp(suffix=".packed", prefix="plc")

        # TODO: Fehlerabfang

        if mode == "zip":
            fh_pack = zipfile.ZipFile(filename, mode="w")
            wd = os.walk("./")
            for tup_dir in wd:
                for file in tup_dir[2]:
                    arcname = os.path.join(
                        os.path.basename(self.plcworkdir), tup_dir[0][2:], file)
                    fh_pack.write(os.path.join(tup_dir[0], file), arcname=arcname)
            if pictory:
                fh_pack.write(configrsc, arcname="config.rsc")
        else:
            fh_pack = tarfile.open(
                name=filename, mode="w:gz", dereference=True)
            fh_pack.add(".", arcname=os.path.basename(self.plcworkdir))
            if pictory:
                fh_pack.add(configrsc, arcname="config.rsc")
        fh_pack.close()
        return filename

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

        if self.plc is not None:
            proginit.logger.debug("stopping revpiplc-thread")
            self.plc.stop()
            self.plc.join()

        if self.xmlrpc:
            proginit.logger.info("shutting down xmlrpc-server")
            self.xsrv.shutdown()
            self.tpe.shutdown()
            self.xsrv.server_close()

    def xml_getconfig(self):
        proginit.logger.debug("xmlrpc call getconfig")
        dc = {}
        dc["autoreload"] = self.autoreload
        dc["autostart"] = self.autostart
        dc["plcworkdir"] = self.plcworkdir
        dc["plcprogram"] = self.plcprog
        dc["plcslave"] = self.plcslave
        dc["xmlrpc"] = self.xmlrpc
        dc["xmlrpcport"] = \
            self.globalconfig["DEFAULT"].get("xmlrpcport", 55123)
        dc["zeroonerror"] = self.zerooneerror
        dc["zeroonexit"] = self.zeroonexit
        return dc

    def xml_getfilelist(self):
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
        proginit.logger.debug("xmlrpc call plcdownload")

        # TODO: Daten blockweise übertragen

        file = self.packapp(mode, pictory)
        if os.path.exists(file):
            fh = open(file, "rb")
            xmldata = Binary(fh.read())
            fh.close()
            os.remove(file)
            return xmldata

    def xml_plcexitcode(self):
        proginit.logger.debug("xmlrpc call plcexitcode")
        if self.plc is None:
            return -2
        elif self.plc.is_alive():
            return -1
        else:
            return self.plc.exitcode

    def xml_plcrunning(self):
        proginit.logger.debug("xmlrpc call plcrunning")
        return False if self.plc is None else self.plc.is_alive()

    def xml_plcstart(self):
        proginit.logger.debug("xmlrpc call plcstart")
        if self.plc is not None and self.plc.is_alive():
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
        if self.plc is not None:
            self.plc.stop()
            self.plc.join()
            return self.plc.exitcode
        else:
            return -1



    def modtarfile(self, fh_pack):
        rootdir = []
        rootfile = []
        for ittar in fh_pack.getmembers():
            fname = ittar.name

            # Illegale Zeichen finden
            if fname.find("/") == 0 or fname.find("..") >= 0:
                return False

            # Führendes ./ entfernen
            ittar.name = fname.replace("./", "")

            # root Elemente finden
            if ittar.isdir() and fname.count("/") == 0:
                if fname not in rootdir:
                    rootdir.append(fname + "/")
            else:
                rootfile.append(fname)

        # Prüfung fertig
        if len(rootdir) == 1:
            if len(rootfile) == 1 and len(rootfile[0]) > 4 \
                    and rootfile[0][-4:].lower() == ".rsc":
                
                # Config.rsc gefunden
                # TODO: config.rsc iwie übergeben
                pass

            # Rootdir abschneiden
            for ittar in fh_pack.getmembers():
                ittar.name = ittar.name.replace(rootdir[0], "")
                proginit.logger.error(ittar.name)

        proginit.logger.error(rootdir)
        return True

    def modzipfile(self, fh_pack):
        # Datei untersuchen
        rootdir = []
        rootfile = []
        for itzip in fh_pack.filelist:
            fname = itzip.filename

            # Illegale Zeichen finden
            if fname.find("/") == 0 or fname.find("..") >= 0:
                return False

            # Führendes ./ entfernen
            itzip.filename = fname.replace("./", "")

            # root Elemente finden
            if fname.count("/") > 0:
                rootname = fname[:fname.find("/") + 1]
                if rootname not in rootdir:
                    rootdir.append(rootname)
            else:
                rootfile.append(fname)

        # Prüfung fertig
        if len(rootdir) == 1 and len(rootfile) >= 1:
            pass



        proginit.logger.error(rootdir)
        return True




    def xml_plcupload(self, filedata):
        proginit.logger.debug("xmlrpc call plcupload")
        noerr = True

        # TODO: Daten blockweise annehmen

        if filedata is None:
            return False

        filename = mktemp(prefix="upl")

        # Daten in tmp-file schreiben
        fh = open(filename, "wb")
        fh.write(filedata.data)
        fh.close()

        # Packer ermitteln
        fh_pack = None
        if tarfile.is_tarfile(filename):
            fh_pack = tarfile.open(filename)
            noerr = self.modtarfile(fh_pack)

        elif zipfile.is_zipfile(filename):
            fh_pack = zipfile.ZipFile(filename)
            noerr = self.modzipfile(fh_pack)

        # Datei entpacken und schließen
        if fh_pack is not None:
            if noerr:
                fh_pack.extractall("./")

            fh_pack.close()

        # Tempdatei löschen
        os.remove(filename)

        return noerr

    def xml_plcuploadclean(self):
        proginit.logger.debug("xmlrpc call plcuploadclean")
        try:
            rmtree(".", ignore_errors=True)
        except:
            return False
        return True

    def xml_reload(self):
        proginit.logger.debug("xmlrpc call reload")
        self.evt_loadconfig.set()

    def xml_setconfig(self, dc, loadnow=False):
        proginit.logger.debug("xmlrpc call setconfig")
        keys = [
            "autoreload",
            "autostart",
            "plcprogram",
            "plcslave",
            "xmlrpc",
            "xmlrpcport",
            "zeroonerror",
            "zeroonexit"
        ]

        # Werte übernehmen
        for key in keys:
            if key in dc:
                self.globalconfig.set("DEFAULT", key, str(dc[key]))

        # conf-Datei schreiben
        fh = open(self.globalconffile, "w")
        self.globalconfig.write(fh)
        proginit.logger.info(
            "got new config and wrote ist to {}".format(self.globalconffile)
        )

        if loadnow:
            # RevPiPyLoad neu konfigurieren
            self.evt_loadconfig.set()

    def xml_setpictoryrsc(self, filebytes, reset=False):
        """Schreibt die config.rsc Datei von piCotry.

        @param filebytes: xmlrpc.client.Binary()-Objekt
        @param reset: Reset piControl Device
        @returns: Statuscode

        """
        proginit.logger.debug("xmlrpc call setpictoryrsc")

        # TODO: Prüfen ob es wirklich eine piCtory Datei ist

        try:
            with open(configrsc, "wb") as fh:
                fh.write(filebytes.data)
        except:
            return -1
        else:
            if reset:
                return os.system(picontrolreset)
            else:
                return 0


if __name__ == "__main__":
    root = RevPiPyLoad()
    root.start()
