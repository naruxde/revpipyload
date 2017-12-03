# -*- coding: utf-8 -*-
#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
"""Modul fuer die Verwaltung der PLC-Slave Funktionen."""
import proginit
import socket
from threading import Event, Thread
from timeit import default_timer
from revpipyload import _ipmatch


class RevPiSlave(Thread):

    """RevPi PLC-Server.

    Diese Klasste stellt den RevPi PLC-Server zur verfuegung und akzeptiert
    neue Verbindungen. Dieser werden dann als RevPiSlaveDev abgebildet.

    Ueber die angegebenen ACLs koennen Zugriffsbeschraenkungen vergeben werden.

    """

    def __init__(self, acl, port=55234):
        """Instantiiert RevPiSlave-Klasse.
        @param acl Stringliste mit Leerstellen getrennt
        @param port Listen Port fuer plc Slaveserver"""
        super().__init__()
        self._evt_exit = Event()
        self.exitcode = None
        self._port = port
        self.so = None
        self._th_dev = []
        self.zeroonerror = False
        self.zeroonexit = False

        # ACLs aufbereiten
        self.dict_acl = {}
        for host in acl.split():
            aclsplit = host.split(",")
            self.dict_acl[aclsplit[0]] = \
                0 if len(aclsplit) == 1 else int(aclsplit[1])

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
            except:
                proginit.logger.warning("can not bind socket - retry")
                self._evt_exit.wait(1)
            else:
                break
        self.so.listen(15)

        # Mit Socket arbeiten
        while not self._evt_exit.is_set():
            self.exitcode = -1

            # Verbindung annehmen
            proginit.logger.debug("accept new connection")
            try:
                tup_sock = self.so.accept()
            except:
                if not self._evt_exit.is_set():
                    proginit.logger.exception("accept exception")
                continue

            # ACL prüfen
            aclstatus = _ipmatch(tup_sock[1][0], self.dict_acl)
            if aclstatus == -1:
                tup_sock[0].close()
                proginit.logger.warning(
                    "host ip '{}' does not match revpiacl - disconnect"
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
            except:
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
            "got new connection from host {} with acl {}".format(
                self._addr, self._acl
            )
        )

        # Prozessabbild öffnen
        try:
            fh_proc = open(proginit.pargs.procimg, "r+b", 0)
        except:
            fh_proc = None
            self._evt_exit.set()
            proginit.logger.error(
                "can not open process image {}".format(proginit.pargs.procimg)
            )

        dirty = True
        while not self._evt_exit.is_set():
            # Laufzeitberechnung starten
            ot = default_timer()

            # Meldung erhalten
            try:
                netcmd = self._devcon.recv(16)
            except:
                break

            # Wenn Meldung ungültig ist aussteigen
            if netcmd[0:1] != b'\x01' or netcmd[-1:] != b'\x17':
                if netcmd != b'':
                    proginit.logger.error(
                        "net cmd not valid {}".format(netcmd)
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
                except:
                    proginit.logger.error("error while send read data")
                    break

            elif cmd == b'SD' and self._acl == 1:
                # Ausgänge setzen, wenn acl es erlaubt
                # bCMiiiic0000000b = 16

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")
                control = netcmd[7:8]

                if control == b'\x1d' and length > 0:
                    # Empfange Datenblock zu schreiben nach Meldung
                    try:
                        block = self._devcon.recv(length)
                    except:
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
                except:
                    proginit.logger.error("can not convert timeout value")
                    break

                if 0 < timeoutms < 65535:
                    self._deadtime = timeoutms / 1000
                    self._devcon.settimeout(self._deadtime)

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                else:
                    proginit.logger.error("timeout value must be 0 to 65535")
                    break

            elif cmd == b'EY':
                # Bytes bei Verbindungsabbruch schreiben
                # bCMiiiix0000000b = 16

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")
                control = netcmd[7:8]

                if control == b'\xFF':
                    # Alle Dirtybytes löschen
                    self.ey_dict = {}

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                    proginit.logger.info("cleared all dirty bytes")

                elif control == b'\xFE':
                    # Bestimmte Dirtybytes löschen

                    if position in self.ey_dict:
                        del self.ey_dict[position]

                    # Record seperator character
                    self._devcon.send(b'\x1e')
                    proginit.logger.info(
                        "cleared dirty bytes on position {}"
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

                    except:
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
                        "got dirty bytes to write on error on position {}"
                        "".format(position)
                    )

            elif cmd == b'PI':
                # piCtory Konfiguration senden
                proginit.logger.debug(
                    "transfair pictory configuration: {}"
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
                        "runtime more than {} ms: {}!".format(
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

        proginit.logger.info("disconnected from {}".format(self._addr))
        proginit.logger.debug("leave RevPiSlaveDev.run()")

    def stop(self):
        """Beendet Verbindungsthread."""
        proginit.logger.debug("enter RevPiSlaveDev.stop()")

        self._evt_exit.set()
        if self._devcon is not None:
            self._devcon.shutdown(socket.SHUT_RDWR)

        proginit.logger.debug("leave RevPiSlaveDev.stop()")
