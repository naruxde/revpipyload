#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
# -*- coding: utf-8 -*-
"""Stellt Funktionen bereit um das Prozessabbild zu ueberwachen.

Bei ausreichend Rechten koennen Ausgaenge auch gesetzt werden um einen
IO-Check bei Inbetriebname durchzufuehren.

"""
import pickle
import revpimodio
from xmlrpc.client import Binary


class ProcimgServer():

    """Serverkomponente fuer zusaetzliche XML-RPC Funktionen.

    Diese Klasse registriert zusaetzliche Funktionen an einem besthenden
    XML-RPC-Server. Der Funktionsumfang wird erweitert um zyklisch das
    Prozessabbild zu empfangen und bei ausreichend Rechten Ausgaenge zu
    setzen.

    """

    def __init__(self, logger, xmlserver, configrsc, procimg, aclmode):
        """Instantiiert RevPiCheckServer()-Klasse.

        @param xmlserver: XML-RPC Server
        @param procimg: Pfad zum Prozessabbild
        @param configrsc: Pfad zur piCtory Konfigurationsdatei
        @param logger: Loggerinstanz
        @param aclmode: Zugriffsrechte

        """
        # Logger übernehmen
        self.logger = logger
        self.logger.debug("enter ProcimgServer.__init__()")
        self.acl = aclmode

        # XML-Server übernehmen
        self.xmlsrv = xmlserver
        self.xmlreadfuncs = {
            "ps_devices": self.devices,
            "ps_inps": lambda: self.ios("inp"),
            "ps_outs": lambda: self.ios("out"),
            "ps_mems": lambda: self.ios("mem"),
            "ps_values": self.values,
        }
        self.xmlwritefuncs = {
            "ps_setvalue": self.setvalue,
        }

        # RevPiModIO-Modul Instantiieren
        self.logger.debug("create revpimodio class")
        self.rpi = revpimodio.RevPiModIO(
            configrsc=configrsc,
            procimg=procimg,
        )
        self.rpi.devices.syncoutputs(device=0)
        self.logger.debug("created revpimodio class")

        self.logger.debug("leave ProcimgServer.__init__()")

    def devices(self):
        """Generiert Deviceliste mit Position und Namen.
        @returns: list() mit Tuple (pos, name)"""
        return [
            (dev.position, dev.name) for dev in self.rpi.devices
        ]

    def ios(self, type):
        """Generiert ein dict() der Devices und IOs.
        @param type: IO Typ inp/out
        @returns: pickled dict()"""
        dict_ios = {}
        for dev in self.rpi.devices:
            dict_ios[dev.position] = []

            # IO Typen auswerten
            if type == "inp":
                lst_io = dev.get_inps()
            elif type == "out":
                lst_io = dev.get_outs()
            elif type == "mem":
                lst_io = dev.get_mems()
            else:
                lst_io = []

            for io in lst_io:
                dict_ios[dev.position].append([
                    io.name,
                    1 if io._bitlength == 1 else int(io._bitlength / 8),
                    io.slc_address.start + dev.offset,
                    io.bmk,
                    io._bitaddress,
                ])
        return Binary(pickle.dumps(dict_ios))

    def setvalue(self, device, io, value):
        """Setzt einen Wert auf dem RevPi.

        @param device: Device Position oder Name
        @param io: IO Name fuer neuen Wert
        @param value: Neuer Wert
        @returns: list() [device, io, status, msg]

        """
        # Zugriffsrechte prüfen
        if self.acl < 3:
            return [
                device, io, False,
                "not allowed in XML-RPC permission mode {}".format(self.acl)
            ]

        # Binary() in bytes() umwandeln
        if type(value) == Binary:
            value = value.data

        self.rpi.devices.syncoutputs(device=device)

        try:
            # Neuen Wert übernehmen
            if type(value) == bytes or type(value) == bool:
                self.rpi.devices[device][io].set_value(value)
            else:
                self.rpi.devices[device][io].set_value(
                    value.to_bytes(
                        self.rpi.devices[device][io].length, byteorder="little"
                    )
                )
        except Exception as e:
            return [device, io, False, str(e)]

        self.rpi.devices.writeprocimg(device=device)
        return [device, io, True, ""]

    def values(self):
        """Liefert Prozessabbild an Client.
        @returns: Binary() bytes or None"""
        if self.rpi.devices.readprocimg() and self.rpi.devices.syncoutputs():
            bytebuff = b''
            for dev in self.rpi.devices:
                bytebuff += bytes(dev)
            return Binary(bytebuff)
        else:
            return None

    def start(self):
        """Registriert XML Funktionen."""
        self.logger.debug("enter ProcimgServer.start()")

        # Registriere Funktionen
        for xmlfunc in self.xmlreadfuncs:
            self.xmlsrv.register_function(self.xmlreadfuncs[xmlfunc], xmlfunc)
        if self.acl >= 3:
            for xmlfunc in self.xmlwritefuncs:
                self.xmlsrv.register_function(
                    self.xmlwritefuncs[xmlfunc], xmlfunc
                )

        self.logger.debug("leave ProcimgServer.start()")

    def stop(self):
        """Entfernt XML-Funktionen."""
        self.logger.debug("enter ProcimgServer.stop()")

        # Entferne Funktionen
        for xmlfunc in self.xmlreadfuncs:
            if xmlfunc in self.xmlsrv.funcs:
                del self.xmlsrv.funcs[xmlfunc]
        if self.acl >= 3:
            for xmlfunc in self.xmlwritefuncs:
                if xmlfunc in self.xmlsrv.funcs:
                    del self.xmlsrv.funcs[xmlfunc]

        self.logger.debug("leave ProcimgServer.stop()")
