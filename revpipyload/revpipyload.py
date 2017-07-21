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
import socket
import subprocess
import tarfile
import zipfile
from concurrent import futures
from configparser import ConfigParser
from json import loads as jloads
from re import fullmatch as refullmatch
from shutil import rmtree
from sys import stdout as sysstdout
from tempfile import mkstemp
from threading import Thread, Event, Lock
from time import sleep, asctime
from timeit import default_timer
from xmlrpc.client import Binary
from xmlrpc.server import SimpleXMLRPCServer

configrsc = None
picontrolreset = "/opt/KUNBUS/piControlReset"
procimg = "/dev/piControl0"
pyloadverion = "0.5.0"
rapcatalog = None

re_ipacl = "(([\\d\\*]{1,3}\\.){3}[\\d\\*]{1,3},[0-1] ?)+"


def _ipmatch(ipaddress, dict_acl):
    """Prueft IP gegen ACL List und gibt ACL aus.

    @param ipaddress zum pruefen
    @param dict_acl ACL Dict gegen die IP zu pruefen ist
    @return int() ACL Wert oder -1 wenn nicht gefunden

    """
    for aclip in sorted(dict_acl, reverse=True):
        regex = aclip.replace(".", "\\.").replace("*", "\\d{1,3}")
        if refullmatch(regex, ipaddress) is not None:
            return str(dict_acl[aclip])
    return "-1"


def _zeroprocimg():
    """Setzt Prozessabbild auf NULL."""
    if os.path.exists(procimg):
        f = open(procimg, "w+b", 0)
        f.write(bytes(4096))


class LogReader():

    """Ermoeglicht den Zugriff auf die Logdateien.

    Beinhaltet Funktionen fuer den Abruf der gesamten Logdatei fuer das
    RevPiPyLoad-System und die Logdatei der PLC-Anwendung.

    """

    def __init__(self):
        """Instantiiert LogReader-Klasse."""
        self.fhapp = None
        self.fhapplk = Lock()
        self.fhplc = None
        self.fhplclk = Lock()

    def closeall(self):
        """Fuehrt close auf File Handler durch."""
        if self.fhapp is not None:
            self.fhapp.close()
        if self.fhplc is not None:
            self.fhplc.close()

    def load_applog(self, start, count):
        """Uebertraegt Logdaten des PLC Programms Binaer.

        @param start Startbyte
        @param count Max. Byteanzahl zum uebertragen
        @return Binary() der Logdatei

        """
        if not os.access(proginit.logapp, os.R_OK):
            return Binary(b'\x16')  # 
        elif start > os.path.getsize(proginit.logapp):
            return Binary(b'\x19')  # 
        else:
            with self.fhapplk:
                if self.fhapp is None or self.fhapp.closed:
                    self.fhapp = open(proginit.logapp, "rb")

                self.fhapp.seek(start)
                return Binary(self.fhapp.read(count))

    def load_plclog(self, start, count):
        """Uebertraegt Logdaten des Loaders Binaer.

        @param start Startbyte
        @param count Max. Byteanzahl zum uebertragen
        @return Binary() der Logdatei

        """
        if not os.access(proginit.logplc, os.R_OK):
            return Binary(b'\x16')  # 
        elif start > os.path.getsize(proginit.logplc):
            return Binary(b'\x19')  # 
        else:
            with self.fhplclk:
                if self.fhplc is None or self.fhplc.closed:
                    self.fhplc = open(proginit.logplc, "rb")

                self.fhplc.seek(start)
                return Binary(self.fhplc.read(count))


class PipeLogwriter(Thread):

    """File PIPE fuer das Schreiben des APP Log.

    Spezieller LogFile-Handler fuer die Ausgabe des subprocess fuer das Python
    PLC Programm. Die Ausgabe kann nicht auf einen neuen FileHandler
    umgeschrieben werden. Dadurch waere es nicht moeglich nach einem logrotate
    die neue Datei zu verwenden. Ueber die PIPE wird dies umgangen.

    """

    def __init__(self, logfilename):
        """Instantiiert PipeLogwriter-Klasse.
        @param logfilename Dateiname fuer Logdatei"""
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
        """Close file handler."""
        if self._fh is not None:
            self._fh.close()

    def _configurefh(self):
        """Konfiguriert den FileHandler fuer Ausgaben der PLCAPP.
        @return FileHandler-Objekt"""
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
        @param message Logzeile zum Schreiben"""
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
        while not self._exit.is_set():
            line = fhread.readline()
            self._lckfh.acquire()
            try:
                self._fh.write(line)
                self._fh.flush()
            except:
                proginit.logger.exception("PipeLogwriter in write log line")
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
        @return PipeLogwriter()"""
        proginit.logger.debug("enter RevPiPlc._configureplw()")
        logfile = None
        if proginit.pargs.daemon:
            if os.access(os.path.dirname(proginit.logapp), os.R_OK | os.W_OK):
                logfile = proginit.logapp
        elif proginit.pargs.logfile is not None:
            logfile = proginit.pargs.logfile

        if logfile is not None:
            logfile = PipeLogwriter(logfile)

        proginit.logger.debug("leave RevPiPlc._configureplw()")
        return logfile

    def _setuppopen(self):
        """Setzt UID und GID fuer das PLC Programm."""
        proginit.logger.info(
            "set uid {} and gid {} for plc program".format(
                self.uid, self.gid)
            )
        os.setgid(self.gid)
        os.setuid(self.uid)

    def _spopen(self, lst_proc):
        """Startet das PLC Programm.
        @param lst_proc Prozessliste
        @return subprocess"""
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
                        "plc program crashed - exitcode: {}".format(
                            self.exitcode
                        )
                    )
                    if self.zeroonerror:
                        _zeroprocimg()
                        proginit.logger.warning(
                            "set piControl0 to ZERO after PLC program error")

                else:
                    # PLC Python Programm sauber beendet
                    proginit.logger.info("plc program did a clean exit")
                    if self.zeroonexit:
                        _zeroprocimg()
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

        if self._plw is not None:
            self._plw.logline("-" * 55)
            self._plw.logline("plc: {} stopped: {}".format(
                os.path.basename(self._program), asctime()
            ))

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
                self._plw.join()
                proginit.logger.debug("log pipes successfully closed")

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
            _zeroprocimg()

        if self._plw is not None:
            self._plw.stop()
            self._plw.join()
            proginit.logger.debug("log pipes successfully closed")

        proginit.logger.debug("leave RevPiPlc.stop()")


class RevPiSlave(Thread):

    def __init__(self, acl, port=55234):
        """Instantiiert RevPiSlave-Klasse.
        @param acl Stringliste mit Leerstellen getrennt
        @param port Listen Port fuer plc Slaveserver"""
        super().__init__()
        self.deadtime = 0.5
        self._evt_exit = Event()
        self.exitcode = None
        self._port = port
        self._th_dev = []
        self.zeroonerror = False
        self.zeroonexit = False

        # ACLs aufbereiten
        self.dict_acl = {}
        for host in acl.split():
            aclsplit = host.split(",")
            self.dict_acl[aclsplit[0]] = \
                "0" if len(aclsplit) == 1 else aclsplit[1]

    def newlogfile(self):
        """Konfiguriert die FileHandler auf neue Logdatei."""
        pass

    def run(self):
        proginit.logger.debug("enter RevPiSlave.run()")

        # Socket öffnen und konfigurieren
        self.so = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        while not self._evt_exit.is_set():
            try:
                self.so.bind(("", self._port))
            except:
                proginit.logger.warning("can not bind socket - retry")
                self._evt_exit.wait(1)
            else:
                break
        self.so.listen(15)

        while not self._evt_exit.is_set():
            self.exitcode = -1

            # Verbindung annehmen
            proginit.logger.debug("accept new connection")
            try:
                tup_sock = self.so.accept()
            except:
                proginit.logger.exception("after accept")
                continue

            # ACL prüfen
            aclstatus = _ipmatch(tup_sock[1][0], self.dict_acl)
            if aclstatus == "-1":
                tup_sock[0].close()
                proginit.logger.warning(
                    "host ip '{}' does not match revpiacl - disconnect"
                    "".format(tup_sock[1][0])
                )
            else:
                # Thread starten
                th = RevPiSlaveDev(tup_sock, self.deadtime, aclstatus)
                th.start()
                self._th_dev.append(th)

            # TODO: tote Threads entfernen

        # Alle Threads beenden
        for th in self._th_dev:
            th.stop()

        # Socket schließen
        self.so.close()
        self.exitcode = 0

        proginit.logger.debug("leave RevPiSlave.run()")

    def stop(self):
        """Beendet Slaveausfuehrung."""
        proginit.logger.debug("enter RevPiSlave.stop()")

        self._evt_exit.set()
        self.so.shutdown(socket.SHUT_RDWR)

        proginit.logger.debug("leave RevPiSlave.stop()")


class RevPiSlaveDev(Thread):

    def __init__(self, devcon, deadtime, acl):
        super().__init__()
        self._acl = acl
        self.daemon = True
        self._deadtime = deadtime
        self._devcon, self._addr = devcon
        self._evt_exit = Event()
        self._startvalr = 0
        self._lenvalr = 0
        self._startvalw = 0
        self._lenvalw = 0

    def run(self):
        proginit.logger.debug("enter RevPiSlaveDev.run()")

        proginit.logger.info(
            "got new connection from host {} with acl {}".format(
                self._addr, self._acl)
        )

        # CMDs anhand ACL aufbauen
        msgcli = [b'DATA', b'PICT']
        if self._acl == "1":
            msgcli.append(b'SEND')

        # Prozessabbild öffnen
        fh_proc = open(procimg, "r+b", 0)

        while not self._evt_exit.is_set():
            # Meldung erhalten
            try:
                netcmd = self._devcon.recv(16)
                # proginit.logger.debug("command {}".format(netcmd))
            except:
                break

            # Laufzeitberechnung starten
            ot = default_timer()

            # Wenn Meldung ungültig ist aussteigen
            cmd = netcmd[:4]
            if cmd not in msgcli:
                break

            if cmd == b'PICT':
                # piCtory Konfiguration senden
                proginit.logger.debug(
                    "transfair pictory configuration: {}".format(configrsc)
                )
                fh_pic = open(configrsc, "rb")
                while True:
                    data = fh_pic.read(1024)
                    if data:
                        self._devcon.send(data)
                    else:
                        fh_pic.close()
                        break

                # Abschlussmeldung
                self._devcon.send(b'PICOK')
                continue

            if cmd == b'DATA':
                # Processabbild übertragen
                # CMD_|POS_|LEN_|RSVE = 16

                position = int(netcmd[4:8])
                length = int(netcmd[8:12])

                fh_proc.seek(position)
                try:
                    self._devcon.sendall(fh_proc.read(length))
                except:
                    break

            if cmd == b'SEND':
                # Ausgänge empfangen
                # CMD_|POS_|LEN_|RSVE = 16

                position = int(netcmd[4:8])
                length = int(netcmd[8:12])

#                try:
                block = self._devcon.recv(length)
#                except:
#                    break
                fh_proc.seek(position)
                fh_proc.write(block)

                # Record seperator
                self._devcon.send(b'\x1e')

            # Verarbeitungszeit prüfen
            comtime = default_timer() - ot
            if comtime > self._deadtime:
                proginit.logger.warning(
                    "runtime more than {} ms: {}".format(
                        int(self._deadtime * 1000), int(comtime * 1000)
                    )
                )
#                break

        fh_proc.close()
        self._devcon.close()
        self._devcon = None

        proginit.logger.info("disconnected from {}".format(self._addr))
        proginit.logger.debug("leave RevPiSlaveDev.run()")

    def stop(self):
        proginit.logger.debug("enter RevPiSlaveDev.stop()")

        self._evt_exit.set()
        if self._devcon is not None:
            self._devcon.shutdown(socket.SHUT_RDWR)

        proginit.logger.debug("leave RevPiSlaveDev.stop()")


class RevPiPyLoad():

    """Hauptklasse, die alle Funktionen zur Verfuegung stellt.

    Hier wird die gesamte Konfiguraiton eingelesen und der ggf. aktivierte
    XML-RPC-Server gestartet.

    """

    def __init__(self):
        """Instantiiert RevPiPyLoad-Klasse."""
        proginit.configure()
        proginit.logger.debug("enter RevPiPyLoad.__init__()")

        # piCtory Konfiguration an bekannten Stellen prüfen
        global configrsc
        lst_rsc = ["/etc/revpi/config.rsc", "/opt/KUNBUS/config.rsc"]
        for rscfile in lst_rsc:
            if os.access(rscfile, os.F_OK | os.R_OK):
                configrsc = rscfile
                break
        if configrsc is None:
            raise RuntimeError(
                "can not find known pictory configurations at {}"
                "".format(", ".join(lst_rsc))
            )

        # rap Katalog an bekannten Stellen prüfen und laden
        global rapcatalog
        lst_rap = [
            "/opt/KUNBUS/pictory/resources/data/rap",
            "/var/www/pictory/resources/data/rap"
        ]
        for rapfolder in lst_rap:
            if os.path.isdir(rapfolder):
                rapcatalog = os.listdir(rapfolder)

        # piControlReset suchen
        global picontrolreset
        if not os.access(picontrolreset, os.F_OK | os.X_OK):
            picontrolreset = "/usr/bin/piTest -x"

        # Klassenattribute
        self._exit = True
        self.pictorymtime = os.path.getmtime(configrsc)
        self.evt_loadconfig = Event()
        self.globalconfig = ConfigParser()
        self.logr = LogReader()
        self.plc = None
        self.tfile = {}
        self.tpe = None
        self.xsrv = None
        self.xml_ps = None

        # Load config
        self._loadconfig()

        # Signal events
        signal.signal(signal.SIGINT, self._sigexit)
        signal.signal(signal.SIGTERM, self._sigexit)
        signal.signal(signal.SIGHUP, self._sigloadconfig)
        signal.signal(signal.SIGUSR1, self._signewlogfile)

        proginit.logger.debug("leave RevPiPyLoad.__init__()")

    def _loadconfig(self):
        """Load configuration file and setup modul."""
        proginit.logger.debug("enter RevPiPyLoad._loadconfig()")

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

        # PLC Slave ACL laden und prüfen
        plcslaveacl = \
            self.globalconfig["DEFAULT"].get("plcslaveacl", "")
        if len(plcslaveacl) > 0 and refullmatch(re_ipacl, plcslaveacl) is None:
            self.plcslaveacl = ""
            proginit.logger.warning("can not load plcslaveacl - wrong format")
        else:
            self.plcslaveacl = plcslaveacl

        self.plcslaveport = \
            int(self.globalconfig["DEFAULT"].get("plcslaveport", 55234))
        self.pythonver = \
            int(self.globalconfig["DEFAULT"].get("pythonversion", 3))
        self.xmlrpc = \
            int(self.globalconfig["DEFAULT"].get("xmlrpc", 0))

        # XML ACL laden und prüfen
        # TODO: xmlrpcacl auswerten
        xmlrpcacl = \
            self.globalconfig["DEFAULT"].get("xmlrpcacl", "")
        if len(xmlrpcacl) > 0 and refullmatch(re_ipacl, xmlrpcacl) is None:
            self.xmlrpcacl = ""
            proginit.logger.warning("can not load xmlrpcacl - wrong format")
        else:
            self.xmlrpcacl = xmlrpcacl

        self.zeroonerror = \
            int(self.globalconfig["DEFAULT"].get("zeroonerror", 1))
        self.zeroonexit = \
            int(self.globalconfig["DEFAULT"].get("zeroonexit", 1))

        # Workdirectory wechseln
        os.chdir(self.plcworkdir)

        # PLC Thread konfigurieren
        self.plc = self._plcthread()
        if self.plcslave:
            self.th_plcslave = RevPiSlave(self.plcslaveacl, self.plcslaveport)
        else:
            self.th_plcslave = None

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
            self.xsrv.register_multicall_functions()

            # XML Modus 1 Nur Logs lesen und PLC Programm neu starten
            self.xsrv.register_function(self.logr.load_applog, "load_applog")
            self.xsrv.register_function(self.logr.load_plclog, "load_plclog")
            self.xsrv.register_function(self.xml_plcexitcode, "plcexitcode")
            self.xsrv.register_function(self.xml_plcrunning, "plcrunning")
            self.xsrv.register_function(self.xml_plcstart, "plcstart")
            self.xsrv.register_function(self.xml_plcstop, "plcstop")
            self.xsrv.register_function(self.xml_reload, "reload")

            # Erweiterte Funktionen anmelden
            try:
                import procimgserver
                self.xml_ps = procimgserver.ProcimgServer(
                    proginit.logger, self.xsrv, configrsc, procimg, self.xmlrpc
                )
                self.xsrv.register_function(self.xml_psstart, "psstart")
                self.xsrv.register_function(self.xml_psstop, "psstop")
            except:
                self.xml_ps = None
                proginit.logger.warning(
                    "can not load revpimodio module. maybe its not installed "
                    "or an old version (required at least 0.15.0). if you "
                    "like to use the process monitor feature, update/install "
                    "revpimodio: 'apt-get install python3-revpimodio'"
                )

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
                    self.xml_plcslavestart, "plcslavestart")
                self.xsrv.register_function(
                    self.xml_plcslavestop, "plcslavestop")
                self.xsrv.register_function(
                    lambda: os.system(picontrolreset), "resetpicontrol")
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

        proginit.logger.debug("leave RevPiPyLoad._loadconfig()")

    def _plcthread(self):
        """Konfiguriert den PLC-Thread fuer die Ausfuehrung.
        @return PLC-Thread Object or None"""
        proginit.logger.debug("enter RevPiPyLoad._plcthread()")
        th_plc = None

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

        proginit.logger.debug("leave RevPiPyLoad._plcthread()")
        return th_plc

    def _sigexit(self, signum, frame):
        """Signal handler to clean and exit program."""
        proginit.logger.debug("enter RevPiPyLoad._sigexit()")

        # Programm stoppen und aufräumen
        self.stop()
        proginit.cleanup()

        proginit.logger.debug("leave RevPiPyLoad._sigexit()")

    def _sigloadconfig(self, signum, frame):
        """Signal handler to load configuration."""
        proginit.logger.debug("enter RevPiPyLoad._sigloadconfig()")
        self.evt_loadconfig.set()
        proginit.logger.debug("leave RevPiPyLoad._sigloadconfig()")

    def _signewlogfile(self, signum, frame):
        """Signal handler to start new logfile."""
        proginit.logger.debug("enter RevPiPyLoad._signewlogfile()")

        # Logger neu konfigurieren
        proginit.configure()
        proginit.logger.warning("start new logfile: {}".format(asctime()))

        # stdout für revpipyplc
        if self.plc is not None:
            self.plc.newlogfile()

        # Logreader schließen
        self.logr.closeall()

        proginit.logger.debug("leave RevPiPyLoad._signewlogfile()")

    def packapp(self, mode="tar", pictory=False):
        """Erzeugt aus dem PLC-Programm ein TAR/Zip-File.

        @param mode Packart 'tar' oder 'zip'
        @param pictory piCtory Konfiguration mit einpacken
        @return Dateinamen des Archivs

        """
        proginit.logger.debug("enter RevPiPyLoad.packapp()")

        tup_file = mkstemp(suffix="_packed", prefix="plc_")
        filename = tup_file[1]

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

        proginit.logger.debug("leave RevPiPyLoad.packapp()")
        return filename

    def start(self):
        """Start revpipyload."""
        proginit.logger.debug("enter RevPiPyLoad.start()")

        proginit.logger.info("starting revpipyload")
        self._exit = False

        if self.xmlrpc >= 1:
            proginit.logger.info("start xmlrpc-server")
            self.tpe = futures.ThreadPoolExecutor(max_workers=1)
            self.tpe.submit(self.xsrv.serve_forever)

        if self.plcslave:
            # Slaveausfuehrung übergeben
            self.th_plcslave.start()

        if self.autostart:
            proginit.logger.debug("starting revpiplc-thread")
            if self.plc is not None:
                self.plc.start()

        while not self._exit \
                and not self.evt_loadconfig.is_set():

            # piCtory auf Veränderung prüfen
            if self.pictorymtime != os.path.getmtime(configrsc):
                proginit.logger.warning("piCtory configuration was changed")
                self.pictorymtime = os.path.getmtime(configrsc)

                if self.xml_ps is not None:
                    self.xml_psstop()
                    self.xml_ps.loadrevpimodio()

            self.evt_loadconfig.wait(1)

        if not self._exit:
            proginit.logger.info("exit python plc program to reload config")
            self._loadconfig()

        proginit.logger.debug("leave RevPiPyLoad.start()")

    def stop(self):
        """Stop revpipyload."""
        proginit.logger.debug("enter RevPiPyLoad.stop()")

        proginit.logger.info("stopping revpipyload")
        self._exit = True

        if self.th_plcslave is not None and self.th_plcslave.is_alive():
            proginit.logger.debug("stopping revpi slave thread")
            self.th_plcslave.stop()
            self.th_plcslave.join()
            proginit.logger.debug("revpi slave thread successfully closed")

        if self.plc is not None and self.plc.is_alive():
            proginit.logger.debug("stopping revpiplc thread")
            self.plc.stop()
            self.plc.join()
            proginit.logger.debug("revpiplc thread successfully closed")

        if self.xmlrpc >= 1:
            proginit.logger.info("shutting down xmlrpc-server")
            self.xsrv.shutdown()
            self.tpe.shutdown()
            self.xsrv.server_close()

        proginit.logger.debug("leave RevPiPyLoad.stop()")

    def xml_getconfig(self):
        """Uebertraegt die RevPiPyLoad Konfiguration.
        @return dict() der Konfiguration"""
        proginit.logger.debug("xmlrpc call getconfig")
        dc = {}
        dc["autoreload"] = self.autoreload
        dc["autostart"] = self.autostart
        dc["plcworkdir"] = self.plcworkdir
        dc["plcprogram"] = self.plcprog
        dc["plcarguments"] = self.plcarguments
        dc["plcslave"] = self.plcslave
        dc["plcslaveacl"] = self.plcslaveacl
        dc["plcslaveport"] = self.plcslaveport
        dc["pythonversion"] = self.pythonver
        dc["xmlrpc"] = self.xmlrpc
        dc["xmlrpcacl"] = self.xmlrpcacl
        dc["xmlrpcport"] = \
            self.globalconfig["DEFAULT"].get("xmlrpcport", 55123)
        dc["zeroonerror"] = self.zeroonerror
        dc["zeroonexit"] = self.zeroonexit
        return dc

    def xml_getfilelist(self):
        """Uebertraegt die Dateiliste vom plcworkdir.
        @return list() mit Dateinamen"""
        proginit.logger.debug("xmlrpc call getfilelist")
        lst_file = []
        wd = os.walk("./")
        for tup_dir in wd:
            for file in tup_dir[2]:
                lst_file.append(os.path.join(tup_dir[0], file)[2:])
        return lst_file

    def xml_getpictoryrsc(self):
        """Gibt die config.rsc Datei von piCotry zurueck.
        @return xmlrpc.client.Binary()"""
        proginit.logger.debug("xmlrpc call getpictoryrsc")
        with open(configrsc, "rb") as fh:
            buff = fh.read()
        return Binary(buff)

    def xml_getprocimg(self):
        """Gibt die Rohdaten aus piControl0 zurueck.
        @return xmlrpc.client.Binary()"""
        proginit.logger.debug("xmlrpc call getprocimg")
        with open(procimg, "rb") as fh:
            buff = fh.read()
        return Binary(buff)

    def xml_plcdownload(self, mode="tar", pictory=False):
        """Uebertraegt ein Archiv vom plcworkdir.

        @param mode Archivart 'tar' 'zip'
        @param pictory piCtory Konfiguraiton mit einpacken
        @return Binary() mit Archivdatei

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

        @return int() exitcode oder:
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
        @return True, wenn das PLC Programm noch lauft"""
        proginit.logger.debug("xmlrpc call plcrunning")
        return False if self.plc is None else self.plc.is_alive()

    def xml_plcstart(self):
        """Startet das PLC Programm.

        @return int() Status:
            -0 Erfolgreich
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

        @return int() Exitcode vom PLC Programm
            -0 Erfolgreich
            -1 PLC Programm lief nicht

        """
        proginit.logger.debug("xmlrpc call plcstop")
        if self.plc is not None and self.plc.is_alive():
            self.plc.stop()
            self.plc.join()
            proginit.logger.debug("revpiplc thread successfully closed")
            return self.plc.exitcode
        else:
            return -1

    def xml_plcupload(self, filedata, filename):
        """Empfaengt Dateien fuer das PLC Programm einzeln.

        @param filedata GZIP Binary data der datei
        @param filename Name inkl. Unterverzeichnis der Datei
        @return Ture, wenn Datei erfolgreich gespeichert wurde

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
        @return True, wenn erfolgreich"""
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
        @return True, wenn erfolgreich angewendet"""
        proginit.logger.debug("xmlrpc call setconfig")
        keys = {
            "autoreload": "[01]",
            "autostart": "[01]",
            "plcprogram": ".+",
            "plcarguments": ".*",
            "plcslave": "[01]",
            "plcslaveacl": re_ipacl,
            "plcslaveport": "[0-9]{,5}",
            "pythonversion": "[23]",
            "xmlrpc": "[0-3]",
            "xmlrpcacl": re_ipacl,
            "xmlrpcport": "[0-9]{,5}",
            "zeroonerror": "[01]",
            "zeroonexit": "[01]"
        }

        # Werte übernehmen
        for key in keys:
            if key in dc:
                if refullmatch(keys[key], str(dc[key])) is None:
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

        @param filebytes xmlrpc.client.Binary()-Objekt
        @param reset Reset piControl Device
        @return Statuscode:
            -0 Alles erfolgreich
            -1 Kann JSON-Datei nicht laden
            -2 piCtory Elemente in JSON-Datei nicht gefunden
            -3 Konnte Konfiguraiton nicht schreiben
            -4 Module in Konfiguration enthalten, die es nicht gibt
            -5 Kein RAP Katalog zur Ueberpruefung gefunden
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

        # Prüfen ob Modulkatalog vorhanden ist
        if rapcatalog is None:
            return -5
        else:

            # piCtory Device in Katalog suchen
            for picdev in jconfigrsc["Devices"]:
                found = False
                picdev = picdev["id"][7:-4]
                for rapdev in rapcatalog:
                    if rapdev.find(picdev) >= 0:
                        found = True

                # Device im Katalog nicht gefunden
                if not found:
                    return -4

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

    def xml_psstart(self):
        """Startet den Prozessabbildserver.
        @return True, wenn start erfolgreich"""
        if self.xml_ps is not None:
            return self.xml_ps.start()
        else:
            return False

    def xml_psstop(self):
        """Stoppt den Prozessabbildserver.
        @return True, wenn stop erfolgreich"""
        if self.xml_ps is not None:
            self.xml_ps.stop()
            return True
        else:
            return False

    def xml_plcslavestart(self):
        """Startet den PLC Slave Server.

        @return Statuscode:
            0: erfolgreich gestartet
            -1: Nicht aktiv in Konfiguration
            -2: Laeuft bereits

        """
        if self.plcslave:
            if self.th_plcslave is not None and self.th_plcslave.is_alive():
                return -2
            else:
                self.th_plcslave = RevPiSlave(
                    self.plcslaveacl, self.plcslaveport
                )
                self.th_plcslave.start()
                return 0
        else:
            return -1

    def xml_plcslavestop(self):
        """Stoppt den PLC Slave Server.
        @return True, wenn stop erfolgreich"""
        if self.th_plcslave is not None:
            self.th_plcslave.stop()
            return True
        else:
            return False


if __name__ == "__main__":
    root = RevPiPyLoad()
    root.start()
