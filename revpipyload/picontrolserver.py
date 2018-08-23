# -*- coding: utf-8 -*-
"""Modul fuer die Verwaltung der PLC-Slave Funktionen."""
__author__ = "Sven Sager"
__copyright__ = "Copyright (C) 2018 Sven Sager"
__license__ = "GPLv3"
import proginit
import socket
from shared.ipaclmanager import IpAclManager
from threading import Event, Thread
from timeit import default_timer


# NOTE: Sollte dies als Process ausgeführt werden?
class RevPiSlave(Thread):

    """RevPi PLC-Server.

    Diese Klasste stellt den RevPi PLC-Server zur verfuegung und akzeptiert
    neue Verbindungen. Dieser werden dann als RevPiSlaveDev abgebildet.

    Ueber die angegebenen ACLs koennen Zugriffsbeschraenkungen vergeben werden.

    """

    def __init__(self, ipacl, port=55234):
        """Instantiiert RevPiSlave-Klasse.
        @param ipacl AclManager <class 'IpAclManager'>
        @param port Listen Port fuer plc Slaveserver"""
        if not type(ipacl) == IpAclManager:
            raise ValueError("parameter ipacl must be <class 'IpAclManager'>")
        if not type(port) == int:
            raise ValueError("parameter port must be <class 'int'>")

        super().__init__()
        self.__ipacl = ipacl
        self._evt_exit = Event()
        self.exitcode = None
        self._port = port
        self.so = None
        self._th_dev = []
        self.zeroonerror = False
        self.zeroonexit = False

    def check_connectedacl(self):
        """Prueft bei neuen ACLs bestehende Verbindungen."""
        for dev in self._th_dev:
            ip,  port = dev._addr
            level = self.__ipacl.get_acllevel(ip)
            if level < 0:
                # Verbindung killen
                proginit.logger.warning(
                    "client {0} not in acl - disconnect!".format(ip)
                )
                dev.stop()
            elif level != dev._acl:
                # ACL Level anpassen
                proginit.logger.warning(
                    "change acl level from {0} to {1} on existing "
                    "connection {2}".format(level, dev._acl, ip)
                )
                dev._acl = level

    def newlogfile(self):
        """Konfiguriert die FileHandler auf neue Logdatei."""
        pass

    def run(self):
        """Startet Serverkomponente fuer die Annahme neuer Verbindungen."""
        proginit.logger.debug("enter RevPiSlave.run()")

        # Socket öffnen und konfigurieren
        self.so = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        while not self._evt_exit.is_set():
            try:
                self.so.bind(("", self._port))
            except Exception:
                proginit.logger.warning("can not bind socket - retry")
                self._evt_exit.wait(1)
            else:
                break
        self.so.listen(15)

        # Mit Socket arbeiten
        while not self._evt_exit.is_set():
            self.exitcode = -1

            # Verbindung annehmen
            proginit.logger.info("accept new connection for revpinetio")
            try:
                tup_sock = self.so.accept()
            except Exception:
                if not self._evt_exit.is_set():
                    proginit.logger.exception("accept exception")
                continue

            # ACL prüfen
            aclstatus = self.__ipacl.get_acllevel(tup_sock[1][0])
            if aclstatus == -1:
                tup_sock[0].close()
                proginit.logger.warning(
                    "host ip '{0}' does not match revpiacl - disconnect"
                    "".format(tup_sock[1][0])
                )
            else:
                # Thread starten
                th = RevPiSlaveDev(tup_sock, aclstatus)
                th.start()
                self._th_dev.append(th)

            # Liste von toten threads befreien
            self._th_dev = [
                th_check for th_check in self._th_dev if th_check.is_alive()
            ]

        # Alle Threads beenden
        for th in self._th_dev:
            th.stop()

        # Socket schließen
        self.so.close()
        self.so = None

        self.exitcode = 0

        proginit.logger.debug("leave RevPiSlave.run()")

    def stop(self):
        """Beendet Slaveausfuehrung."""
        proginit.logger.debug("enter RevPiSlave.stop()")

        self._evt_exit.set()
        if self.so is not None:
            try:
                self.so.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

        proginit.logger.debug("leave RevPiSlave.stop()")


class RevPiSlaveDev(Thread):

    """Klasse um eine RevPiModIO Verbindung zu verwalten.

    Diese Klasste stellt die Funktionen zur Verfuegung um Daten ueber das
    Netzwerk mit dem Prozessabbild auszutauschen.

    """

    def __init__(self, devcon, acl):
        """Init RevPiSlaveDev-Class.

        @param devcon Tuple der Verbindung
        @param deadtime Timeout der Vararbeitung
        @param acl Berechtigungslevel

        """
        super().__init__()
        self._acl = acl
        self.daemon = True
        self._deadtime = None
        self._devcon, self._addr = devcon
        self._evt_exit = Event()
        self._writeerror = False

        # Sicherheitsbytes
        self.ey_dict = {}

    def run(self):
        """Verarbeitet Anfragen von Remoteteilnehmer."""
        proginit.logger.debug("enter RevPiSlaveDev.run()")

        proginit.logger.info(
            "got new connection from host {0} with acl {1}".format(
                self._addr, self._acl
            )
        )

        # Prozessabbild öffnen
        try:
            fh_proc = open(proginit.pargs.procimg, "r+b", 0)
        except Exception:
            fh_proc = None
            self._evt_exit.set()
            proginit.logger.error(
                "can not open process image {0}".format(proginit.pargs.procimg)
            )

        dirty = True
        while not self._evt_exit.is_set():
            # Laufzeitberechnung starten
            ot = default_timer()

            # Meldung erhalten
            try:
                netcmd = self._devcon.recv(16)
            except Exception:
                break

            # Wenn Meldung ungültig ist aussteigen
            if netcmd[0:1] != b'\x01' or netcmd[-1:] != b'\x17':
                if netcmd != b'':
                    proginit.logger.error(
                        "net cmd not valid {0}".format(netcmd)
                    )
                break

            cmd = netcmd[1:3]
            if cmd == b'DA':
                # Processabbild übertragen
                # bCMiiii00000000b = 16

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")

                fh_proc.seek(position)
                try:
                    self._devcon.sendall(fh_proc.read(length))
                except Exception:
                    proginit.logger.error("error while send read data")
                    break

            elif cmd == b'SD':
                # Ausgänge setzen, wenn acl es erlaubt
                # bCMiiiic0000000b = 16

                # Berechtigung prüfen und ggf. trennen
                if self._acl < 1:
                    self._devcon.send(b'\x18')
                    break

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")
                control = netcmd[7:8]

                if control == b'\x1d' and length > 0:
                    # Empfange Datenblock zu schreiben nach Meldung
                    try:
                        block = self._devcon.recv(length)
                    except Exception:
                        proginit.logger.error("error while recv data to write")
                        self._writeerror = True
                        break
                    fh_proc.seek(position)

                    # Länge der Daten prüfen
                    if len(block) == length:
                        fh_proc.write(block)
                    else:
                        proginit.logger.error("got wrong length to write")
                        break

                # Record seperator character
                if control == b'\x1c':
                    # Bestätige Schreibvorgang aller Datenblöcke
                    if self._writeerror:
                        self._devcon.send(b'\xff')
                    else:
                        self._devcon.send(b'\x1e')
                    self._writeerror = False

            elif cmd == b'\x06\x16':
                # Just sync
                self._devcon.send(b'\x06\x16')

            elif cmd == b'CF':
                # Socket konfigurieren
                # bCMii0000000000b = 16

                try:
                    timeoutms = int.from_bytes(netcmd[3:5], byteorder="little")
                except Exception:
                    proginit.logger.error("can not convert timeout value")
                    break

                if 0 < timeoutms < 65535:
                    self._deadtime = timeoutms / 1000
                    self._devcon.settimeout(self._deadtime)
                    proginit.logger.debug(
                        "set socket timeout to {0}".format(self._deadtime)
                    )

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                else:
                    proginit.logger.error("timeout value must be 0 to 65535")
                    self._devcon.send(b'\xff')
                    break

            elif cmd == b'EY':
                # Bytes bei Verbindungsabbruch schreiben
                # bCMiiiix0000000b = 16

                # Berechtigung prüfen und ggf. trennen
                if self._acl < 1:
                    self._devcon.send(b'\x18')
                    break

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")
                control = netcmd[7:8]

                if control == b'\xff':
                    # Alle Dirtybytes löschen
                    self.ey_dict = {}

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                    proginit.logger.info("cleared all dirty bytes")

                elif control == b'\xfe':
                    # Bestimmte Dirtybytes löschen

                    if position in self.ey_dict:
                        del self.ey_dict[position]

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                    proginit.logger.info(
                        "cleared dirty bytes on position {0}"
                        "".format(position)
                    )

                else:
                    # Dirtybytes hinzufügen
                    bytesbuff = bytearray()
                    try:
                        while not self._evt_exit.is_set() \
                                and len(bytesbuff) < length:
                            block = self._devcon.recv(1024)
                            bytesbuff += block
                            if block == b'':
                                break

                    except Exception:
                        proginit.logger.error("error while recv dirty bytes")
                        break

                    # Länge der Daten prüfen
                    if len(bytesbuff) == length:
                        self.ey_dict[position] = bytesbuff
                    else:
                        proginit.logger.error("got wrong length to write")
                        break

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                    proginit.logger.info(
                        "got dirty bytes to write on error on position {0}"
                        "".format(position)
                    )

            elif cmd == b'PI':
                # piCtory Konfiguration senden
                proginit.logger.debug(
                    "transfair pictory configuration: {0}"
                    "".format(proginit.pargs.configrsc)
                )
                fh_pic = open(proginit.pargs.configrsc, "rb")
                while True:
                    data = fh_pic.read(1024)
                    if data:
                        # FIXME: Fehler fangen
                        self._devcon.send(data)
                    else:
                        fh_pic.close()
                        break

                # End-of-Transmission character
                self._devcon.send(b'\x04')
                continue

            elif cmd == b'EX':
                # Sauber Verbindung verlassen
                dirty = False
                self._evt_exit.set()
                continue

            else:
                # Kein gültiges CMD gefunden, abbruch!
                break

            # Verarbeitungszeit prüfen
            if self._deadtime is not None:
                comtime = default_timer() - ot
                if comtime > self._deadtime:
                    proginit.logger.warning(
                        "runtime more than {0} ms: {1}!".format(
                            int(self._deadtime * 1000), comtime
                        )
                    )
                    # TODO: Soll ein Fehler ausgelöst werden?

        # Dirty verlassen
        if dirty:
            for pos in self.ey_dict:
                fh_proc.seek(pos)
                fh_proc.write(self.ey_dict[pos])

            proginit.logger.error("dirty shutdown of connection")

        if fh_proc is not None:
            fh_proc.close()
        self._devcon.close()
        self._devcon = None

        proginit.logger.info("disconnected from {0}".format(self._addr))
        proginit.logger.debug("leave RevPiSlaveDev.run()")

    def stop(self):
        """Beendet Verbindungsthread."""
        proginit.logger.debug("enter RevPiSlaveDev.stop()")

        self._evt_exit.set()
        if self._devcon is not None:
            self._devcon.shutdown(socket.SHUT_RDWR)

        proginit.logger.debug("leave RevPiSlaveDev.stop()")
