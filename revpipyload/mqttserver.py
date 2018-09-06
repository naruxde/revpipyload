# -*- coding: utf-8 -*-
"""Stellt die MQTT Uebertragung fuer IoT-Zwecke bereit."""
__author__ = "Sven Sager"
__copyright__ = "Copyright (C) 2018 Sven Sager"
__license__ = "GPLv3"
import proginit
import revpimodio2
from os.path import join
from paho.mqtt.client import Client, connack_string
from ssl import CERT_NONE
from threading import Thread, Event


class MqttServer(Thread):

    """Server fuer die Uebertragung des Prozessabbilds per MQTT."""

    def __init__(
            self, basetopic, sendinterval, broker_address, port=1883,
            tls_set=False, username="", password=None, client_id="",
            send_events=False, write_outputs=False):
        """Init MqttServer class.

        @param basetopic Basis-Topic fuer Datenaustausch
        @param sendinterval Prozessabbild alle n Sekunden senden
        @param broker_address Adresse <class 'str'> des MQTT-Servers
        @param port Portnummer <class 'int'> des MQTT-Servers
        @param tls_set TLS fuer Verbindung zum MQTT-Server verwenden
        @param username Optional Benutzername fuer MQTT-Server
        @param password Optional Password fuer MQTT-Server
        @param client_id MQTT ClientID, wenn leer automatisch random erzeugung
        @param send_events Sendet Werte bei IO Wertaenderung
        @param write_outputs Per MQTT auch Outputs schreiben

        """
        if not isinstance(basetopic, str):
            raise ValueError("parameter topic must be <class 'str'>")
        if not (isinstance(sendinterval, int) and sendinterval > 0):
            raise ValueError(
                "parameter sendinterval must be <class 'int'> and > 0"
            )
        if not (isinstance(broker_address, str) and broker_address != ""):
            raise ValueError(
                "parameter broker_address must be <class 'str'> and not empty"
            )
        if not (isinstance(port, int) and 0 < port < 65535):
            raise ValueError(
                "parameter sendinterval must be <class 'int'> and 1 - 65535"
            )
        if not isinstance(tls_set, bool):
            raise ValueError("parameter tls_set must be <class 'bool'>")
        if not isinstance(username, str):
            raise ValueError("parameter username must be <class 'str'>")
        if not (password is None or isinstance(password, str)):
            raise ValueError("parameter password must be <class 'str'>")
        if not isinstance(client_id, str):
            raise ValueError("parameter client_id must be <class 'str'>")
        if not isinstance(send_events, bool):
            raise ValueError("parameter send_events must be <class 'bool'>")
        if not isinstance(write_outputs, bool):
            raise ValueError("parameter write_outputs must be <class 'bool'>")

        super().__init__()

        # Klassenvariablen
        self.__exit = False
        self._evt_data = Event()
        self._exported_ios = []
        self._broker_address = broker_address
        self._port = port
        self._reloadmodio = False
        self._rpi = None
        self._rpi_write = None
        self._send_events = send_events
        self._sendinterval = sendinterval
        self._write_outputs = write_outputs

        # RevPiModIO laden oder mit Exception aussteigen
        self._loadrevpimodio()

        # Topics konfigurieren
        self._mqtt_evt_io = join(basetopic, "event/{0}")
        self._mqtt_io = join(basetopic, "io/{0}")
        self._mqtt_ioset = join(basetopic, "set/#")
        self._mqtt_pictory = join(basetopic, "pictory")
        self._mqtt_sendpictory = join(basetopic, "needpictory")

        self._mq = Client(client_id)
        if username != "":
            self._mq.username_pw_set(username, password)
        if tls_set:
            self._mq.tls_set(cert_reqs=CERT_NONE)
            self._mq.tls_insecure_set(True)

        # Handler konfigurieren
        self._mq.on_connect = self._on_connect
        self._mq.on_message = self._on_message

    def _evt_io(self, name, value):
        """Sendet Daten aus Events.
        @param name IO-Name
        @param value IO-Value"""
        if isinstance(value, bytes):
            value = int.from_bytes(value, "little")
        self._mq.publish(self._mqtt_evt_io.format(name), int(value))

    def _loadrevpimodio(self):
        """Instantiiert das RevPiModIO Modul.
        @return None or Exception"""
        self._reloadmodio = False
        self._exported_ios = []

        # RevPiModIO-Modul Instantiieren
        if self._rpi is not None:
            self._rpi.cleanup()
        if self._rpi_write is not None:
            self._rpi_write.cleanup()

        proginit.logger.debug("create revpimodio2 object for MQTT")
        try:
            # Lesend und Eventüberwachung
            self._rpi = revpimodio2.RevPiModIO(
                autorefresh=self._send_events,
                monitoring=True,
                configrsc=proginit.pargs.configrsc,
                procimg=proginit.pargs.procimg
            )

            # Schreibenen Zugriff
            if self._write_outputs:
                self._rpi_write = revpimodio2.RevPiModIO(
                    configrsc=proginit.pargs.configrsc,
                    procimg=proginit.pargs.procimg
                )

        except Exception as e:
            self._rpi = None
            self._rpi_write = None
            proginit.logger.error(
                "piCtory configuration not loadable for MQTT"
            )
            raise e

        # Exportierte IOs laden
        for dev in self._rpi.device:
            for io in dev.get_allios(export=True):
                io.reg_event(self._evt_io)
                self._exported_ios.append(io)

        # Eventüberwachung starten
        if self._send_events:
            self._rpi.mainloop(blocking=False)

        proginit.logger.debug("created revpimodio2 object")

    def _on_connect(self, client, userdata, flags, rc):
        """Verbindung zu MQTT Broker."""
        if rc > 0:
            proginit.logger.warning(
                "can not connect to mqtt broker '{0}' - error '{1}' - "
                "will retry".format(self._broker_address, connack_string(rc))
            )
        else:
            # Subscribe piCtory Anforderung
            client.subscribe(self._mqtt_sendpictory)
            if self._write_outputs:
                client.subscribe(self._mqtt_ioset)

    def _on_disconnect(self, client, userdata, rc):
        """Wertet Verbindungsabbruch aus."""
        if rc != 0:
            proginit.logger.warning(
                "unexpected disconnection from mqtt broker - "
                "will try to reconnect"
            )

    def _on_message(self, client, userdata, msg):
        """Sendet piCtory Konfiguration."""
        if msg.topic == self._mqtt_pictory:
            # piCtory Konfiguration senden
            self._send_pictory_conf()

        else:
            lst_topic = msg.topic.split("/")
            ioname = lst_topic[-1]

            try:
                io = self._rpi_write.io[ioname]
                io_needbytes = type(io.value) == bytes
            except Exception:
                proginit.logger.error(
                    "can not find io '{0}' for MQTT".format(ioname)
                )
                return

            # Check Output exists and is an Output
            if io.type != revpimodio2.OUT:
                proginit.logger.error(
                    "can not write to inputs with MQTT"
                )
            elif not io.export:
                proginit.logger.error(
                    "io '{0}' is not marked as export in piCtory for MQTT use"
                    "".format(ioname)
                )
            else:

                # Convert MQTT Payload to valid Output-Value
                value = msg.payload.decode("utf8")

                if value.isdecimal():
                    value = int(value)

                    # Muss eine Byteumwandlung vorgenommen werden?
                    if io_needbytes:
                        try:
                            value = value.to_bytes(io.length, io.byteorder)
                        except OverflowError:
                            proginit.logger.error(
                                "can not convert value '{0}' to fitting bytes"
                                "".format(value)
                            )
                            return

                elif value == "false" and not io_needbytes:
                    value = 0
                elif value == "true" and not io_needbytes:
                    value = 1
                else:
                    proginit.logger.error(
                        "can not convert value '{0}' for output '{1}'"
                        "".format(value, ioname)
                    )
                    return

                # Write Value to RevPi
                io._parentdevice.syncoutputs()
                try:
                    io.value = value
                except Exception:
                    proginit.logger.error(
                        "could not write '{0}' to Output '{1}'"
                        "".format(value, ioname)
                    )
                else:
                    io._parentdevice.writeprocimg()

    def _send_pictory_conf(self):
        """Sendet piCtory Konfiguration per MQTT."""
        try:
            fh = open(proginit.pargs.configrsc, "rb")
            self._mq.publish(self._mqtt_pictory, fh.read())
            fh.close()
        except Exception:
            proginit.logger.error(
                "can not read and publish piCtory config '{0}'"
                "".format(proginit.pargs.configrsc)
            )

    def newlogfile(self):
        """Konfiguriert die FileHandler auf neue Logdatei."""
        pass

    def reload_revpimodio(self):
        """Fuehrt im naechsten Zyklus zum Reload."""
        self._reloadmodio = True

    def run(self):
        """Startet die Uebertragung per MQTT."""
        proginit.logger.debug("enter MqttServer.run()")

        # MQTT verbinden
        try:
            self._mq.connect(self._broker_address, self._port, keepalive=60)
        except Exception:
            self._on_connect(self._mq, None, None, 3)
            self._mq.connect_async(
                self._broker_address, self._port, keepalive=60
            )
        self._mq.loop_start()

        # mainloop
        while not self.__exit:
            self._evt_data.clear()

            # RevPiModIO neu laden
            if self._reloadmodio:
                self._loadrevpimodio()

            # Werte laden, wenn nicht autorefresh
            if not self._send_events:
                self._rpi.readprocimg()

            # Exportierte IOs übertragen
            for io in self._exported_ios:
                value = io.value
                if isinstance(value, bytes):
                    value = int.from_bytes(value, "little")
                self._mq.publish(self._mqtt_io.format(io.name), int(value))

            self._evt_data.wait(self._sendinterval)

        # MQTT trennen
        self._mq.loop_stop()
        self._mq.disconnect()

        # RevPiModIO aufräumen
        self._rpi.cleanup()

        proginit.logger.debug("leave MqttServer.run()")

    def stop(self):
        """Stoppt die Uebertragung per MQTT."""
        proginit.logger.debug("enter MqttServer.stop()")
        self.__exit = True
        self._evt_data.set()
        proginit.logger.debug("leave MqttServer.stop()")
