#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Revolution Pi Python PLC Loader.

Webpage: https://revpimodio.org/revpipyplc/

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
__author__ = "Sven Sager"
__copyright__ = "Copyright (C) 2018 Sven Sager"
__license__ = "GPLv3"
__version__ = "0.7.0"
import gzip
import logsystem
import picontrolserver
import plcsystem
import proginit
import os
import signal
import tarfile
import zipfile
from configparser import ConfigParser
from helper import refullmatch
from json import loads as jloads
from shared.ipaclmanager import IpAclManager
from shutil import rmtree
from tempfile import mkstemp
from threading import Event
from time import asctime
from xmlrpc.client import Binary
from xrpcserver import SaveXMLRPCServer


class RevPiPyLoad():

    """Hauptklasse, die alle Funktionen zur Verfuegung stellt.

    Hier wird die gesamte Konfiguraiton eingelesen und der ggf. aktivierte
    XML-RPC-Server gestartet.

    """

    def __init__(self):
        """Instantiiert RevPiPyLoad-Klasse."""
        proginit.logger.debug("enter RevPiPyLoad.__init__()")

        # Klassenattribute
        self._exit = True
        self.pictorymtime = os.path.getmtime(proginit.pargs.configrsc)
        self.evt_loadconfig = Event()
        self.globalconfig = ConfigParser()
        self.logr = logsystem.LogReader()
        self.xsrv = None
        self.xml_ps = None

        # Berechtigungsmanger
        self.plcslaveacl = IpAclManager(minlevel=0, maxlevel=1)
        self.xmlrpcacl = IpAclManager(minlevel=0, maxlevel=4)

        # Threads/Prozesse
        self.th_plcmqtt = None
        self.th_plcslave = None
        self.plc = None

        # Konfiguration laden
        self._loadconfig()

        # Signal events
        signal.signal(signal.SIGINT, self._sigexit)
        signal.signal(signal.SIGTERM, self._sigexit)
        signal.signal(signal.SIGHUP, self._sigloadconfig)
        signal.signal(signal.SIGUSR1, self._signewlogfile)

        proginit.logger.debug("leave RevPiPyLoad.__init__()")

    def _check_mustrestart_mqtt(self):
        """Prueft ob sich kritische Werte veraendert haben.
        @return True, wenn Subsystemneustart noetig ist"""
        if self.th_plcmqtt is None:
            return True
        elif "MQTT" not in self.globalconfig:
            return True
        else:
            return (
                self.mqtt !=
                self.globalconfig["MQTT"].getboolean("mqtt", False) or
                self.mqttbasetopic !=
                self.globalconfig["MQTT"].get("basetopic", "") or
                self.mqttsendinterval !=
                self.globalconfig["MQTT"].getint("sendinterval", 30) or
                self.mqtthost !=
                self.globalconfig["MQTT"].get("host", "") or
                self.mqttport !=
                self.globalconfig["MQTT"].getint("port", 1883) or
                self.mqtttls_set !=
                self.globalconfig["MQTT"].getboolean("tls_set", False) or
                self.mqttusername !=
                self.globalconfig["MQTT"].get("username", "") or
                self.mqttpassword !=
                self.globalconfig["MQTT"].get("password", "") or
                self.mqttclient_id !=
                self.globalconfig["MQTT"].get("client_id", "") or
                self.mqttsend_events !=
                self.globalconfig["MQTT"].getboolean("send_on_event", False) or
                self.mqttwrite_outputs !=
                self.globalconfig["MQTT"].getboolean("write_outputs", False)
            )

    def _check_mustrestart_plcslave(self):
        """Prueft ob sich kritische Werte veraendert haben.
        @return True, wenn Subsystemneustart noetig ist"""
        if self.th_plcslave is None:
            return True
        elif "PLCSLAVE" not in self.globalconfig:
            return True
        else:
            ip = self.globalconfig["PLCSLAVE"].get("bindip", "127.0.0.1")
            if ip == "*":
                ip = ""
            elif ip == "":
                ip = "127.0.0.1"
            port = self.globalconfig["PLCSLAVE"].getint("port", 55234)

            return (
                self.plcslave !=
                self.globalconfig["PLCSLAVE"].getboolean("plcslave", False) or
                self.plcslavebindip != ip or
                self.plcslaveport != port
            )

    def _check_mustrestart_plcprogram(self):
        """Prueft ob sich kritische Werte veraendert haben.
        @return True, wenn Subsystemneustart noetig ist"""
        if self.plc is None:
            return True
        elif "XMLRPC" not in self.globalconfig:
            return True
        else:
            return (
                self.plcworkdir !=
                self.globalconfig["DEFAULT"].get("plcworkdir", ".") or
                self.plcprogram !=
                self.globalconfig["DEFAULT"].get("plcprogram", "none.py") or
                self.plcarguments !=
                self.globalconfig["DEFAULT"].get("plcarguments", "") or
                self.plcuid !=
                self.globalconfig["DEFAULT"].getint("plcuid", 65534) or
                self.plcgid !=
                self.globalconfig["DEFAULT"].getint("plcgid", 65534) or
                self.pythonversion !=
                self.globalconfig["DEFAULT"].getint("pythonversion", 3) or
                self.rtlevel !=
                self.globalconfig["DEFAULT"].getint("rtlevel", 0)
            )

    def _loadconfig(self):
        """Load configuration file and setup modul."""
        proginit.logger.debug("enter RevPiPyLoad._loadconfig()")

        # Subsysteme herunterfahren
        self.stop_xmlrpcserver()

        # Konfigurationsdatei laden
        proginit.logger.info(
            "loading config file: {0}".format(proginit.globalconffile)
        )
        self.globalconfig.read(proginit.globalconffile)

        # Merker für Subsystem-Neustart nach laden, vor setzen
        restart_plcmqtt = self._check_mustrestart_mqtt()
        restart_plcslave = self._check_mustrestart_plcslave()
        restart_plcprogram = self._check_mustrestart_plcprogram()

        # Konfiguration verarbeiten [DEFAULT]
        self.autoreload = \
            self.globalconfig["DEFAULT"].getboolean("autoreload", True)
        self.autoreloaddelay = \
            self.globalconfig["DEFAULT"].getint("autoreloaddelay", 5)
        self.autostart = \
            self.globalconfig["DEFAULT"].getboolean("autostart", False)
        self.plcworkdir = \
            self.globalconfig["DEFAULT"].get("plcworkdir", ".")
        self.plcprogram = \
            self.globalconfig["DEFAULT"].get("plcprogram", "none.py")
        self.plcarguments = \
            self.globalconfig["DEFAULT"].get("plcarguments", "")
        self.plcuid = \
            self.globalconfig["DEFAULT"].getint("plcuid", 65534)
        self.plcgid = \
            self.globalconfig["DEFAULT"].getint("plcgid", 65534)
        self.pythonversion = \
            self.globalconfig["DEFAULT"].getint("pythonversion", 3)
        self.rtlevel = \
            self.globalconfig["DEFAULT"].getint("rtlevel", 0)
        self.zeroonerror = \
            self.globalconfig["DEFAULT"].getboolean("zeroonerror", True)
        self.zeroonexit = \
            self.globalconfig["DEFAULT"].getboolean("zeroonexit", True)

        # Konfiguration verarbeiten [MQTT]
        self.mqtt = 0
        if "MQTT" in self.globalconfig:
            self.mqtt = \
                self.globalconfig["MQTT"].getboolean("mqtt", False)
            self.mqttbasetopic = \
                self.globalconfig["MQTT"].get("basetopic", "")
            self.mqttsendinterval = \
                self.globalconfig["MQTT"].getint("sendinterval", 30)
            self.mqtthost = \
                self.globalconfig["MQTT"].get("host", "")
            self.mqttport = \
                self.globalconfig["MQTT"].getint("port", 1883)
            self.mqtttls_set = \
                self.globalconfig["MQTT"].getboolean("tls_set", False)
            self.mqttusername = \
                self.globalconfig["MQTT"].get("username", "")
            self.mqttpassword = \
                self.globalconfig["MQTT"].get("password", "")
            self.mqttclient_id = \
                self.globalconfig["MQTT"].get("client_id", "")
            self.mqttsend_events = \
                self.globalconfig["MQTT"].getboolean("send_on_event", False)
            self.mqttwrite_outputs = \
                self.globalconfig["MQTT"].getboolean("write_outputs", False)

        # Konfiguration verarbeiten [PLCSLAVE]
        self.plcslave = False
        if "PLCSLAVE" in self.globalconfig:
            self.plcslave = \
                self.globalconfig["PLCSLAVE"].getboolean("plcslave", False)

            # Berechtigungen laden
            if not self.plcslaveacl.loadaclfile(
                    self.globalconfig["PLCSLAVE"].get("aclfile", "")):
                proginit.logger.warning(
                    "can not load plcslave acl - wrong format"
                )
            if not self.plcslave:
                self.stop_plcslave()

            # Bind IP lesen und anpassen
            self.plcslavebindip = \
                self.globalconfig["PLCSLAVE"].get("bindip", "127.0.0.1")
            if self.plcslavebindip == "*":
                self.plcslavebindip = ""
            elif self.plcslavebindip == "":
                self.plcslavebindip = "127.0.0.1"

            self.plcslaveport = \
                self.globalconfig["PLCSLAVE"].getint("port", 55234)

        # Konfiguration verarbeiten [XMLRPC]
        self.xmlrpc = False
        if "XMLRPC" in self.globalconfig:
            self.xmlrpc = \
                self.globalconfig["XMLRPC"].getboolean("xmlrpc", False)

            if not self.xmlrpcacl.loadaclfile(
                    self.globalconfig["XMLRPC"].get("aclfile", "")):
                proginit.logger.warning(
                    "can not load xmlrpc acl - wrong format"
                )

            # Bind IP lesen und anpassen
            self.xmlrpcbindip = \
                self.globalconfig["XMLRPC"].get("bindip", "127.0.0.1")
            if self.xmlrpcbindip == "*":
                self.xmlrpcbindip = ""
            elif self.xmlrpcbindip == "":
                self.xmlrpcbindip = "127.0.0.1"

            self.xmlrpcport = \
                self.globalconfig["XMLRPC"].getint("port", 55123)

        # Workdirectory wechseln
        if not os.access(self.plcworkdir, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError(
                "can not access plcworkdir '{0}'".format(self.plcworkdir)
            )
        os.chdir(self.plcworkdir)

        # MQTT konfigurieren
        if restart_plcmqtt:
            self.stop_plcmqtt()
            self.th_plcmqtt = self._plcmqtt()

            if not self._exit and self.th_plcmqtt is not None:
                proginit.logger.info("restart mqtt publisher after reload")
                self.th_plcmqtt.start()

        # PLC Programm konfigurieren
        if restart_plcprogram:
            self.stop_plcprogram()
            self.plc = self._plcthread()

            if not self._exit and self.plc is not None and self.autostart:
                proginit.logger.info("restart plc program after reload")
                self.plc.start()

        else:
            proginit.logger.info(
                "configure plc program parameters after reload"
            )
            self.plc.autoreload = self.autoreload
            self.plc.autoreloaddelay = self.autoreloaddelay
            self.plc.zeroonerror = self.zeroonerror
            self.plc.zeroonexit = self.zeroonexit

        # PLC-Slave konfigurieren
        if restart_plcslave:
            self.stop_plcslave()
            self.th_plcslave = self._plcslave()

            if not self._exit and self.th_plcslave is not None:
                proginit.logger.info("restart plc slave after reload")
                self.th_plcslave.start()

        # PLC-Slave ACL prüfen
        if self.th_plcslave is not None:
            self.th_plcslave.check_connectedacl()

        # XMLRPC-Server Instantiieren und konfigurieren
        if not self.xmlrpc:
            # Nach Reload und Deaktivierung alte XML Instanz löschen
            self.xsrv = None
        else:
            proginit.logger.debug("create xmlrpc server")
            self.xsrv = SaveXMLRPCServer(
                (self.xmlrpcbindip, self.xmlrpcport),
                logRequests=False,
                allow_none=True,
                ipacl=self.xmlrpcacl
            )
            self.xsrv.register_introspection_functions()
            self.xsrv.register_multicall_functions()

            # Allgemeine Funktionen
            self.xsrv.register_function(0, lambda: __version__, "version")
            self.xsrv.register_function(0, lambda acl: acl, "xmlmodus")

            # XML Modus 1 Nur Logs lesen und PLC Programm neu starten
            self.xsrv.register_function(
                0, self.logr.load_applog, "load_applog")
            self.xsrv.register_function(
                0, self.logr.load_plclog, "load_plclog")
            self.xsrv.register_function(
                0, self.xml_plcexitcode, "plcexitcode")
            self.xsrv.register_function(
                0, self.xml_plcrunning, "plcrunning")
            self.xsrv.register_function(
                0, self.xml_plcstart, "plcstart")
            self.xsrv.register_function(
                0, self.xml_plcstop, "plcstop")
            self.xsrv.register_function(
                0, self.xml_reload, "reload")
            self.xsrv.register_function(
                0, self.xml_mqttrunning, "mqttrunning")
            self.xsrv.register_function(
                0, self.xml_plcslaverunning, "plcslaverunning")

            # Erweiterte Funktionen anmelden
            try:
                import procimgserver
                self.xml_ps = procimgserver.ProcimgServer(self.xsrv)
                self.xsrv.register_function(1, self.xml_psstart, "psstart")
                self.xsrv.register_function(1, self.xml_psstop, "psstop")
            except Exception:
                self.xml_ps = None
                proginit.logger.warning(
                    "can not load revpimodio2 module. maybe its not installed "
                    "or an old version (required at least 2.2.3). if you "
                    "like to use the process monitor feature, update/install "
                    "revpimodio2: 'apt-get install python3-revpimodio2'"
                )

            # XML Modus 2 Einstellungen lesen und Programm herunterladen
            self.xsrv.register_function(
                2, self.xml_getconfig, "get_config")
            self.xsrv.register_function(
                2, self.xml_getfilelist, "get_filelist")
            self.xsrv.register_function(
                2, self.xml_getpictoryrsc, "get_pictoryrsc")
            self.xsrv.register_function(
                2, self.xml_getprocimg, "get_procimg")
            self.xsrv.register_function(
                2, self.xml_plcdownload, "plcdownload")

            # XML Modus 3 Programm und Konfiguration hochladen
            self.xsrv.register_function(
                3, self.xml_plcupload, "plcupload")
            self.xsrv.register_function(
                3, self.xml_plcuploadclean, "plcuploadclean")
            self.xsrv.register_function(
                3,
                lambda: os.system(proginit.picontrolreset),
                "resetpicontrol"
            )
            self.xsrv.register_function(
                3, self.xml_mqttstart, "mqttstart")
            self.xsrv.register_function(
                3, self.xml_mqttstop, "mqttstop")
            self.xsrv.register_function(
                3, self.xml_plcslavestart, "plcslavestart")
            self.xsrv.register_function(
                3, self.xml_plcslavestop, "plcslavestop")

            # XML Modus 4 Einstellungen ändern
            self.xsrv.register_function(
                4, self.xml_setconfig, "set_config")
            self.xsrv.register_function(
                4, self.xml_setpictoryrsc, "set_pictoryrsc")

            proginit.logger.debug("created xmlrpc server")

            # Neustart bei reload
            if not self._exit:
                proginit.logger.info("start xmlrpc-server")
                self.xsrv.start()

        # Konfiguration abschließen
        self.evt_loadconfig.clear()

        proginit.logger.debug("leave RevPiPyLoad._loadconfig()")

    def _plcmqtt(self):
        """Konfiguriert den MQTT-Thread fuer die Ausfuehrung.
        @return MQTT-Thread Object or None"""
        proginit.logger.debug("enter RevPiPyLoad._plcmqtt()")

        th_plc = None
        if self.mqtt:
            try:
                from mqttserver import MqttServer
                th_plc = MqttServer(
                    self.mqttbasetopic,
                    self.mqttsendinterval,
                    self.mqtthost,
                    self.mqttport,
                    self.mqtttls_set,
                    self.mqttusername,
                    self.mqttpassword,
                    self.mqttclient_id,
                    self.mqttsend_events,
                    self.mqttwrite_outputs,
                )
            except Exception:
                proginit.logger.warning(
                    "can not load revpimodio2 module. maybe its not installed "
                    "or an old version (required at least 2.2.3). if you "
                    "like to use the mqtt feature, update/install "
                    "revpimodio2: 'apt-get install python3-revpimodio2'"
                )

        proginit.logger.debug("leave RevPiPyLoad._plcmqtt()")
        return th_plc

    def _plcthread(self):
        """Konfiguriert den PLC-Thread fuer die Ausfuehrung.
        @return PLC-Thread Object or None"""
        proginit.logger.debug("enter RevPiPyLoad._plcthread()")
        th_plc = None

        # Prüfen ob Programm existiert
        if not os.path.exists(os.path.join(self.plcworkdir, self.plcprogram)):
            proginit.logger.error("plc file does not exists {0}".format(
                os.path.join(self.plcworkdir, self.plcprogram)
            ))
            return None

        proginit.logger.debug("create PLC program watcher")
        th_plc = plcsystem.RevPiPlc(
            os.path.join(self.plcworkdir, self.plcprogram),
            self.plcarguments,
            self.pythonversion
        )
        th_plc.autoreload = self.autoreload
        th_plc.autoreloaddelay = self.autoreloaddelay
        th_plc.gid = int(self.plcgid)
        th_plc.uid = int(self.plcuid)
        th_plc.rtlevel = int(self.rtlevel)
        th_plc.zeroonerror = self.zeroonerror
        th_plc.zeroonexit = self.zeroonexit

        proginit.logger.debug("leave RevPiPyLoad._plcthread()")
        return th_plc

    def _plcslave(self):
        """Erstellt den PlcSlave-Server Thread.
        @return PLC-Server-Thread Object or None"""
        proginit.logger.debug("enter RevPiPyLoad._plcslave()")
        th_plc = None

        if self.plcslave:
            th_plc = picontrolserver.RevPiSlave(
                self.plcslaveacl, self.plcslaveport
            )

        proginit.logger.debug("leave RevPiPyLoad._plcslave()")
        return th_plc

    def _sigexit(self, signum, frame):
        """Signal handler to clean and exit program."""
        proginit.logger.debug("enter RevPiPyLoad._sigexit()")
        self.stop()
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
        proginit.logger.warning("start new logfile: {0}".format(asctime()))

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
                    fh_pack.write(
                        proginit.pargs.configrsc, arcname="config.rsc"
                    )
            except Exception:
                filename = ""
            finally:
                fh_pack.close()

        else:
            fh_pack = tarfile.open(
                name=filename, mode="w:gz", dereference=True)
            try:
                fh_pack.add(".", arcname=os.path.basename(self.plcworkdir))
                if pictory:
                    fh_pack.add(proginit.pargs.configrsc, arcname="config.rsc")
            except Exception:
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

        if self.xmlrpc and self.xsrv is not None:
            proginit.logger.info("start xmlrpc-server")
            self.xsrv.start()

        # MQTT Uebertragung starten
        if self.th_plcmqtt is not None:
            self.th_plcmqtt.start()

        # Slaveausfuehrung übergeben
        if self.th_plcslave is not None:
            self.th_plcslave.start()

        # PLC Programm automatisch starten
        if self.autostart and self.plc is not None:
            self.plc.start()

        # mainloop
        while not self._exit:

            # Neue Konfiguration laden
            if self.evt_loadconfig.is_set():
                proginit.logger.info("got reqeust to reload config")
                self._loadconfig()

            # MQTT Publisher Thread prüfen
            if self.mqtt and self.th_plcmqtt is not None \
                    and not self.th_plcmqtt.is_alive():
                proginit.logger.warning(
                    "restart mqtt publisher after thread was not running"
                )
                self.th_plcmqtt = self._plcmqtt()
                if self.th_plcmqtt is not None:
                    self.th_plcmqtt.start()

            # PLC Server Thread prüfen
            if self.plcslave and self.th_plcslave is not None \
                    and not self.th_plcslave.is_alive():
                proginit.logger.warning(
                    "restart plc slave after thread was not running"
                )
                self.th_plcslave = self._plcslave()
                if self.th_plcslave is not None:
                    self.th_plcslave.start()

            # piCtory auf Veränderung prüfen
            if self.pictorymtime != os.path.getmtime(proginit.pargs.configrsc):
                proginit.logger.warning("piCtory configuration was changed")
                self.pictorymtime = os.path.getmtime(proginit.pargs.configrsc)

                # MQTT Publisher neu laden
                if self.mqtt and self.th_plcmqtt is not None:
                    self.th_plcmqtt.reload_revpimodio()

                # XML Prozessabbildserver neu laden
                if self.xml_ps is not None:
                    self.xml_psstop()
                    self.xml_ps.loadrevpimodio()

            self.evt_loadconfig.wait(1)

        proginit.logger.info("stopping revpipyload")

        # Alle Sub-Systeme beenden
        self.stop_plcprogram()
        self.stop_plcmqtt()
        self.stop_plcslave()
        self.stop_xmlrpcserver()

        # Logreader schließen
        self.logr.closeall()

        proginit.logger.debug("leave RevPiPyLoad.start()")

    def stop(self):
        """Stop revpipyload."""
        proginit.logger.debug("enter RevPiPyLoad.stop()")
        self._exit = True
        proginit.logger.debug("leave RevPiPyLoad.stop()")

    def stop_plcmqtt(self):
        """Beendet MQTT Sender."""
        proginit.logger.debug("enter RevPiPyLoad.stop_plcmqtt()")

        if self.th_plcmqtt is not None and self.th_plcmqtt.is_alive():
            proginit.logger.info("stopping mqtt thread")
            self.th_plcmqtt.stop()
            self.th_plcmqtt.join()
            proginit.logger.debug("mqtt thread successfully closed")

        proginit.logger.debug("leave RevPiPyLoad.stop_plcmqtt()")

    def stop_plcprogram(self):
        """Beendet PLC Programm."""
        proginit.logger.debug("enter RevPiPyLoad.stop_plcprogram()")

        if self.plc is not None and self.plc.is_alive():
            proginit.logger.info("stopping revpiplc thread")
            self.plc.stop()
            self.plc.join()
            proginit.logger.debug("revpiplc thread successfully closed")

        proginit.logger.debug("leave RevPiPyLoad.stop_plcprogram()")

    def stop_plcslave(self):
        """Beendet PLC Slave."""
        proginit.logger.debug("enter RevPiPyLoad.stop_plcslave()")

        if self.th_plcslave is not None and self.th_plcslave.is_alive():
            proginit.logger.info("stopping revpi slave thread")
            self.th_plcslave.stop()
            self.th_plcslave.join()
            proginit.logger.debug("revpi slave thread successfully closed")

        proginit.logger.debug("leave RevPiPyLoad.stop_plcslave()")

    def stop_xmlrpcserver(self):
        """Beendet XML-RPC."""
        proginit.logger.debug("enter RevPiPyLoad.stop_xmlrpcserver()")

        if self.xsrv is not None:
            proginit.logger.info("shutting down xmlrpc-server")
            self.xsrv.stop()

        proginit.logger.debug("leave RevPiPyLoad.stop_xmlrpcserver()")

    def xml_getconfig(self):
        """Uebertraegt die RevPiPyLoad Konfiguration.
        @return dict() der Konfiguration"""
        proginit.logger.debug("xmlrpc call getconfig")
        dc = {}

        # DEFAULT Sektion
        dc["autoreload"] = self.autoreload
        dc["autoreloaddelay"] = self.autoreloaddelay
        dc["autostart"] = self.autostart
        dc["plcworkdir"] = self.plcworkdir
        dc["plcprogram"] = self.plcprogram
        dc["plcarguments"] = self.plcarguments
        # dc["plcuid"] = self.plcuid
        # dc["plcgid"] = self.plcgid
        dc["pythonversion"] = self.pythonversion
        dc["rtlevel"] = self.rtlevel
        dc["zeroonerror"] = self.zeroonerror
        dc["zeroonexit"] = self.zeroonexit

        # MQTT Sektion
        dc["mqtt"] = self.mqtt
        dc["mqttbasetopic"] = self.mqttbasetopic
        dc["mqttsendinterval"] = self.mqttsendinterval
        dc["mqtthost"] = self.mqtthost
        dc["mqttport"] = self.mqttport
        dc["mqtttls_set"] = self.mqtttls_set
        dc["mqttusername"] = self.mqttusername
        dc["mqttpassword"] = self.mqttpassword
        dc["mqttclient_id"] = self.mqttclient_id
        dc["mqttsend_events"] = self.mqttsend_events
        dc["mqttwrite_outputs"] = self.mqttwrite_outputs

        # PLCSLAVE Sektion
        dc["plcslave"] = self.plcslave
        dc["plcslaveacl"] = self.plcslaveacl.acl
        dc["plcslavebindip"] = self.plcslavebindip
        dc["plcslaveport"] = self.plcslaveport

        # XMLRPC Sektion
        dc["xmlrpc"] = self.xmlrpc
        dc["xmlrpcacl"] = self.xmlrpcacl.acl
        dc["xmlrpcbindip"] = self.xmlrpcbindip

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
        with open(proginit.pargs.configrsc, "rb") as fh:
            buff = fh.read()
        return Binary(buff)

    def xml_getprocimg(self):
        """Gibt die Rohdaten aus piControl0 zurueck.
        @return xmlrpc.client.Binary()"""
        proginit.logger.debug("xmlrpc call getprocimg")
        with open(proginit.pargs.procimg, "rb") as fh:
            buff = fh.read()
        return Binary(buff)

    def xml_mqttrunning(self):
        """Prueft ob MQTT Uebertragung noch lauft.
        @return True, wenn MQTT Uebertragung noch lauft"""
        proginit.logger.debug("xmlrpc call mqttrunning")
        return False if self.th_plcmqtt is None \
            else self.th_plcmqtt.is_alive()

    def xml_mqttstart(self):
        """Startet die MQTT Uebertragung.

        @return Statuscode:
            0: erfolgreich gestartet
            -1: Nicht aktiv in Konfiguration
            -2: Laeuft bereits

        """
        if self.th_plcmqtt is not None and self.th_plcmqtt.is_alive():
            return -2
        else:
            self.th_plcmqtt = self._plcmqtt()
            if self.th_plcmqtt is None:
                return -1
            else:
                self.th_plcmqtt.start()
                return 0

    def xml_mqttstop(self):
        """Stoppt  die MQTT Uebertragung.
        @return True, wenn stop erfolgreich"""
        if self.th_plcmqtt is not None:
            self.stop_plcmqtt()
            self.th_plcmqtt = None
            return True
        else:
            return False

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
            with open(file, "rb") as fh:
                xmldata = Binary(fh.read())
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
            self.stop_plcprogram()
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

        # Windowszeichen prüfen
        filename = filename.replace("\\", "/")

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
        except Exception:
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
            "DEFAULT": {
                "autoreload": "[01]",
                "autoreloaddelay": "[0-9]+",
                "autostart": "[01]",
                "plcprogram": ".+",
                "plcarguments": ".*",
                # "plcuid": "[0-9]{,5}",
                # "plcgid": "[0-9]{,5}",
                "pythonversion": "[23]",
                "rtlevel": "[0-1]",
                "zeroonerror": "[01]",
                "zeroonexit": "[01]",
            },
            "MQTT": {
                "mqtt": "[01]",
                "mqttbasetopic": ".*",
                "mqttsendinterval": "[0-9]+",
                "mqtthost": ".+",
                "mqttport": "[0-9]+",
                "mqtttls_set": "[01]",
                "mqttusername": ".*",
                "mqttpassword": ".*",
                "mqttclient_id": ".*",
                "mqttsend_events": "[01]",
                "mqttwrite_outputs": "[01]",
            },
            "PLCSLAVE": {
                "plcslave": "[01]",
                "plcslaveacl": self.plcslaveacl.regex_acl,
                # "plcslavebindip": "^((([\\d]{1,3}\\.){3}[\\d]{1,3})|\\*)+$",
                "plcslaveport": "[0-9]{,5}",
            },
            "XMLRPC": {
                "xmlrpc": "[01]",
                "xmlrpcacl": self.xmlrpcacl.regex_acl,
                # "xmlrpcbindip": "^((([\\d]{1,3}\\.){3}[\\d]{1,3})|\\*)+$",
                # "xmlslaveport": "[0-9]{,5}",
            }
        }

        # Werte übernehmen
        for sektion in keys:
            suffix = sektion.lower()
            for key in keys[sektion]:
                if key in dc:
                    localkey = key.replace(suffix, "")
                    if not refullmatch(keys[sektion][key], str(dc[key])):
                        proginit.logger.error(
                            "got wrong setting '{0}' with value '{1}'".format(
                                key, dc[key]
                            )
                        )
                        return False
                    if localkey != "acl":
                        self.globalconfig.set(
                            sektion,
                            key if localkey == "" else localkey,
                            str(dc[key])
                        )

        # conf-Datei schreiben
        with open(proginit.globalconffile, "w") as fh:
            self.globalconfig.write(fh)
        proginit.logger.info(
            "got new config and wrote it to {0}"
            "".format(proginit.globalconffile)
        )

        # ACLs sofort übernehmen und schreiben
        str_acl = dc.get("plcslaveacl", None)
        if str_acl is not None and self.plcslaveacl.acl != str_acl:
            self.plcslaveacl.acl = str_acl
            if not self.plcslaveacl.writeaclfile(aclname="PLC-SLAVE"):
                proginit.logger.error(
                    "can not write acl file '{0}' for PLC-SLAVE"
                    "".format(self.plcslaveacl.filename)
                )
                return False
            else:
                proginit.logger.info(
                    "wrote new acl file '{0}' for PLC-SLAVE"
                    "".format(self.plcslaveacl.filename)
                )
        str_acl = dc.get("xmlrpcacl", None)
        if str_acl is not None and self.xmlrpcacl.acl != str_acl:
            self.xmlrpcacl.acl = str_acl
            if not self.xmlrpcacl.writeaclfile(aclname="XML-RPC"):
                proginit.logger.error(
                    "can not write acl file '{0}' for XML-RPC"
                    "".format(self.xmlrpcacl.filename)
                )
                return False
            else:
                proginit.logger.info(
                    "wrote new acl file '{0}' for XML-RPC"
                    "".format(self.xmlrpcacl.filename)
                )

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
        except Exception:
            return -1

        # Elemente prüfen
        lst_check = ["Devices", "Summary", "App"]
        for chk in lst_check:
            if chk not in jconfigrsc:
                return -2

        # Prüfen ob Modulkatalog vorhanden ist
        if proginit.rapcatalog is None:
            return -5
        else:

            # piCtory Device in Katalog suchen
            for picdev in jconfigrsc["Devices"]:
                found = False
                picdev = picdev["id"][7:-4]
                for rapdev in proginit.rapcatalog:
                    if rapdev.find(picdev) >= 0:
                        found = True

                # Device im Katalog nicht gefunden
                if not found:
                    return -4

        try:
            with open(proginit.pargs.configrsc, "wb") as fh:
                fh.write(filebytes.data)
        except Exception:
            return -3
        else:
            if reset:
                return os.system(proginit.picontrolreset)
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

    def xml_plcslaverunning(self):
        """Prueft ob PLC-Slave noch lauft.
        @return True, wenn PLC-Slave noch lauft"""
        proginit.logger.debug("xmlrpc call plcslaverunning")
        return False if self.th_plcslave is None \
            else self.th_plcslave.is_alive()

    def xml_plcslavestart(self):
        """Startet den PLC Slave Server.

        @return Statuscode:
            0: erfolgreich gestartet
            -1: Nicht aktiv in Konfiguration
            -2: Laeuft bereits

        """
        if self.th_plcslave is not None and self.th_plcslave.is_alive():
            return -2
        else:
            self.th_plcslave = self._plcslave()
            if self.th_plcslave is None:
                return -1
            else:
                self.th_plcslave.start()
                return 0

    def xml_plcslavestop(self):
        """Stoppt den PLC Slave Server.
        @return True, wenn stop erfolgreich"""
        if self.th_plcslave is not None:
            self.stop_plcslave()
            self.th_plcslave = None
            return True
        else:
            return False


if __name__ == "__main__":
    # Programmeinstellungen konfigurieren
    proginit.configure()

    # Programm starten
    root = RevPiPyLoad()
    root.start()

    # Aufräumen
    proginit.cleanup()
