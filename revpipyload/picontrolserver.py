# -*- coding: utf-8 -*-
"""Modul fuer die Verwaltung der PLC-Slave Funktionen."""
__author__ = "Sven Sager"
__copyright__ = "Copyright (C) 2018 Sven Sager"
__license__ = "GPLv3"
import proginit
import socket
from fcntl import ioctl
from shared.ipaclmanager import IpAclManager
from threading import Event, Thread
from timeit import default_timer

# Hashvalues
HASH_NULL = b'\x00' * 16
HASH_FAIL = b'\xff' * 16
HASH_PICT = HASH_FAIL
HASH_RPIO = HASH_NULL


class RevPiSlave(Thread):

    """RevPi PLC-Server.

    Diese Klasste stellt den RevPi PLC-Server zur verfuegung und akzeptiert
    neue Verbindungen. Diese werden dann als RevPiSlaveDev abgebildet.

    Ueber die angegebenen ACLs koennen Zugriffsbeschraenkungen vergeben werden.

    """

    def __init__(self, ipacl, port=55234, bindip=""):
        """Instantiiert RevPiSlave-Klasse.

        @param ipacl AclManager <class 'IpAclManager'>
        @param port Listen Port fuer plc Slaveserver
        @param bindip IP-Adresse an die der Dienst gebunden wird (leer=alle)

        """
        if not isinstance(ipacl, IpAclManager):
            raise ValueError("parameter ipacl must be <class 'IpAclManager'>")
        if not (isinstance(port, int) and 0 < port <= 65535):
            raise ValueError(
                "parameter port must be <class 'int'> and in range 1 - 65535"
            )
        if not isinstance(bindip, str):
            raise ValueError("parameter bindip must be <class 'str'>")

        super().__init__()
        self.__ipacl = ipacl
        self._bindip = bindip
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

    def disconnect_all(self):
        """Close all device connection."""
        # Alle Threads beenden
        for th in self._th_dev:
            th.stop()

    def disconnect_replace_ios(self):
        """Close all device with loaded replace_ios file."""
        # Alle Threads beenden die Replace_IOs emfpangen haben
        for th in self._th_dev:
            if th.got_replace_ios:
                th.stop()

    def newlogfile(self):
        """Konfiguriert die FileHandler auf neue Logdatei."""
        pass

    def run(self):
        """Startet Serverkomponente fuer die Annahme neuer Verbindungen."""
        proginit.logger.debug("enter RevPiSlave.run()")

        # Socket öffnen und konfigurieren bis Erfolg oder Ende
        self.so = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.so.settimeout(2)
        while not self._evt_exit.is_set():
            try:
                self.so.bind((self._bindip, self._port))
            except Exception as e:
                proginit.logger.warning(
                    "can not bind socket: {0} - retry".format(e)
                )
                self._evt_exit.wait(1)
            else:
                self.so.listen(32)
                break

        # Mit Socket arbeiten
        while not self._evt_exit.is_set():
            self.exitcode = -1

            # Verbindung annehmen
            try:
                tup_sock = self.so.accept()
                proginit.logger.info("accepted new connection for revpinetio")
            except socket.timeout:
                continue
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
        @param acl Berechtigungslevel

        """
        super().__init__()
        self.__doerror = False
        self._acl = acl
        self.daemon = True
        self._deadtime = None
        self._devcon, self._addr = devcon
        self._evt_exit = Event()
        self.got_replace_ios = False
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

        buff_size = 2048
        dirty = True
        netcmd = bytearray()
        buff_block = bytearray(buff_size)
        buff_recv = bytearray()
        while not self._evt_exit.is_set():
            # Laufzeitberechnung starten
            ot = default_timer()
            netcmd.clear()

            # Meldung erhalten
            try:
                recv_len = 16
                while recv_len > 0:
                    count = self._devcon.recv_into(buff_block, recv_len)
                    if count == 0:
                        raise IOError("lost network connection")
                    netcmd += buff_block[:count]
                    recv_len -= count
            except Exception as e:
                proginit.logger.exception(e)
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
                    self._devcon.sendall(b'\x18')
                    break

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")
                control = netcmd[7:8]

                if control == b'\x1d' and length > 0:
                    # Empfange Datenblock zu schreiben nach Meldung
                    buff_recv.clear()
                    try:
                        while length > 0:
                            count = self._devcon.recv_into(buff_block, min(length, buff_size))
                            if count == 0:
                                raise IOError("lost network connection")
                            buff_recv += buff_block[:count]
                            length -= count
                    except Exception:
                        proginit.logger.error("error while recv data to write")
                        self._writeerror = True
                        break
                    fh_proc.seek(position)
                    fh_proc.write(buff_recv)

                # Record seperator character
                if control == b'\x1c':
                    # Bestätige Schreibvorgang aller Datenblöcke
                    if self._writeerror:
                        self._devcon.sendall(b'\xff')
                    else:
                        self._devcon.sendall(b'\x1e')
                    self._writeerror = False

            elif cmd == b'\x06\x16':
                # Just sync
                self._devcon.sendall(b'\x06\x16')

            elif cmd == b'CF':
                # Socket konfigurieren
                # bCMii0000000000b = 16

                try:
                    timeoutms = int.from_bytes(netcmd[3:5], byteorder="little")
                except Exception:
                    proginit.logger.error("can not convert timeout value")
                    break

                if 0 < timeoutms <= 65535:
                    self._deadtime = timeoutms / 1000
                    self._devcon.settimeout(self._deadtime)
                    proginit.logger.debug(
                        "set socket timeout to {0}".format(self._deadtime)
                    )

                    # Record seperator character
                    self._devcon.sendall(b'\x1e')
                else:
                    proginit.logger.error("timeout value must be 0 to 65535")
                    self._devcon.sendall(b'\xff')
                    break

            elif cmd == b'EY':
                # Bytes bei Verbindungsabbruch schreiben
                # bCMiiiix0000000b = 16

                # Berechtigung prüfen und ggf. trennen
                if self._acl < 1:
                    self._devcon.sendall(b'\x18')
                    break

                position = int.from_bytes(netcmd[3:5], byteorder="little")
                length = int.from_bytes(netcmd[5:7], byteorder="little")
                control = netcmd[7:8]

                ok_byte = b'\xff' if self.__doerror else b'\x1e'

                if control == b'\xff':
                    # Alle Dirtybytes löschen
                    self.ey_dict = {}

                    # Record seperator character
                    self._devcon.sendall(ok_byte)
                    proginit.logger.info("cleared all dirty bytes")

                elif control == b'\xfe':
                    # Bestimmte Dirtybytes löschen

                    if position in self.ey_dict:
                        del self.ey_dict[position]

                    # Record seperator character
                    self._devcon.sendall(ok_byte)
                    proginit.logger.info(
                        "cleared dirty bytes on position {0}"
                        "".format(position)
                    )

                else:
                    # Dirtybytes hinzufügen
                    buff_recv.clear()
                    try:
                        while length > 0:
                            count = self._devcon.recv_into(buff_block, min(length, buff_size))
                            if count == 0:
                                raise IOError("lost network connection")
                            length -= count
                            buff_recv += buff_block[:count]

                    except Exception:
                        proginit.logger.error("error while recv dirty bytes")
                        break

                    self.ey_dict[position] = bytes(buff_recv)

                    # Record seperator character
                    self._devcon.sendall(ok_byte)
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
                try:
                    with open(proginit.pargs.configrsc, "rb") as fh_pic:
                        # Komplette piCtory Datei senden
                        self._devcon.sendall(fh_pic.read())
                except Exception as e:
                    proginit.logger.error(
                        "error on pictory transfair: {0}".format(e)
                    )
                    break
                else:
                    # End-of-Transmission character immer senden
                    self._devcon.sendall(b'\x04')
                    continue

            elif cmd == b'PH':
                # piCtory md5 Hashwert senden (16 Byte)
                proginit.logger.debug(
                    "send pictory hashvalue: {0}".format(HASH_PICT)
                )
                self._devcon.sendall(HASH_PICT)

            elif cmd == b'RP':
                # Replace_IOs Konfiguration senden, wenn hash existiert
                proginit.logger.debug(
                    "transfair replace_io configuration: {0}"
                    "".format(proginit.pargs.configrsc)
                )
                replace_ios = proginit.conf["DEFAULT"].get("replace_ios", "")
                try:
                    if HASH_RPIO != HASH_NULL and replace_ios:
                        with open(replace_ios, "rb") as fh:
                            # Komplette replace_io Datei senden
                            self._devcon.sendall(fh.read())
                except Exception as e:
                    proginit.logger.error(
                        "error on replace_io transfair: {0}".format(e)
                    )
                    break
                else:
                    # End-of-Transmission character immer senden
                    self._devcon.sendall(b'\x04')
                    continue

            elif cmd == b'RH':
                # Replace_IOs md5 Hashwert senden (16 Byte)
                self.got_replace_ios = True

                proginit.logger.debug(
                    "send replace_ios hashvalue: {0}".format(HASH_RPIO)
                )
                self._devcon.sendall(HASH_RPIO)

            elif cmd == b'EX':
                # Sauber Verbindung verlassen
                dirty = False
                self._evt_exit.set()
                continue

            elif cmd == b'IC':
                # Net-IOCTL ausführen
                # bCMiiiiii000000b = 16

                request = int.from_bytes(netcmd[3:7], byteorder="little")
                length = int.from_bytes(netcmd[7:9], byteorder="little")

                buff_recv.clear()
                try:
                    while length > 0:
                        count = self._devcon.recv_into(buff_block, min(length, buff_size))
                        if count == 0:
                            raise IOError("lost network connection")
                        length -= count
                        buff_recv += buff_block[:count]
                except Exception:
                    proginit.logger.error("error on network ioctl call")
                    break

                # Berechtigung prüfen und ggf. trennen
                if self._acl < 1:
                    self._devcon.sendall(b'\x18')
                    break

                try:
                    if proginit.pargs.procimg == "/dev/piControl0":
                        # Läuft auf RevPi
                        ioctl(fh_proc, request, bytes(buff_recv))
                        proginit.logger.debug(
                            "ioctl {0} with {1} successful"
                            "".format(request, bytes(buff_recv))
                        )
                    else:
                        # Simulation
                        # TODO: IOCTL für Dateien implementieren
                        proginit.logger.warning(
                            "ioctl {0} with {1} simulated".format(request, bytes(buff_recv))
                        )
                except Exception as ex:
                    proginit.logger.error(ex)
                    self._devcon.sendall(b'\xff')
                else:
                    self._devcon.sendall(b'\x1e')

            elif proginit.pargs.developermode and cmd == b'DV':
                # Development options
                # bCMc00000000000b = 16
                if self._acl < 9:
                    # Spezieller ACL-Wert für Entwicklung
                    self._devcon.sendall(b'\x18')
                    break

                c = netcmd[3:4]
                if c == b'a':
                    # CMD a = Switch ACL to 0
                    self._acl = 0
                    proginit.logger.warning("DV: set acl to 0")
                    self._devcon.sendall(b'\x1e')
                elif c == b'b':
                    # CMD b = Aktiviert/Deaktiviert den Fehlermodus
                    if netcmd[4:5] == b'\x01':
                        self.__doerror = True
                        proginit.logger.warning("DV: set do_error")
                    else:
                        self.__doerror = False
                        proginit.logger.warning("DV: reset do_error")
                    self._devcon.sendall(b'\x1e')

            else:
                # Kein gültiges CMD gefunden, abbruch!
                proginit.logger.error(
                    "found unknown net cmd: {0}".format(cmd)
                )
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
