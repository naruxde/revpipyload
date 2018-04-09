# -*- coding: utf-8 -*-
#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
"""Stellt die MQTT Uebertragung fuer IoT-Zwecke bereit."""
import proginit
from json import load as jload
from ssl import CERT_NONE
from paho.mqtt.client import Client
from threading import Thread, Event


class MqttServer(Thread):

    """Server fuer die Uebertragung des Prozessabbilds per MQTT."""

    def __init__(
            self, basetopic, sendinterval, host, port=1883,
            tls_set=False, username="", password=None, client_id=""):
        """Init MqttServer class.

        @param basetopic Basis-Topic fuer Datenaustausch
        @param sendinterval Prozessabbild alle n Sekunden senden
        @param host Adresse <class 'str'> des MQTT-Servers
        @param port Portnummer <class 'int'> des MQTT-Servers
        @param keepalive MQTT Ping bei leerlauf
        @param tls_set TLS fuer Verbindung zum MQTT-Server verwenden
        @param username Optional Benutzername fuer MQTT-Server
        @param password Optional Password fuer MQTT-Server
        @param client_id MQTT ClientID, wenn leer automatisch random erzeugung

        """
        # TODO: Parameterprüfung

        super().__init__()

        # Klassenvariablen
        self.__exit = False
        self._evt_data = Event()
        self._host = host
        self._procimglength = self._get_procimglength()
        self._port = port
        self._sendinterval = sendinterval

        # Topics konfigurieren
        self._mqtt_picontrol = "{}/picontrol".format(basetopic)
        self._mqtt_pictory = "{}/pictory".format(basetopic)
        self._mqtt_sendpictory = "{}/needpictory".format(basetopic)

        self._mq = Client(client_id)
        if username != "":
            self._mq.username_pw_set(username, password)
        if tls_set:
            self._mq.tls_set(cert_reqs=CERT_NONE)
            self._mq.tls_insecure_set(True)

        # Handler konfigurieren
        self._mq.on_connect = self._on_connect
        self._mq.on_message = self._on_message
        # TODO: self._mq.on_disconnect = self._on_disconnect

    def _get_procimglength(self):
        """Ermittelt aus piCtory Konfiguraiton die laenge.
        @return Laenge des Prozessabbilds <class 'int'>"""
        try:
            with open(proginit.pargs.configrsc, "r") as fh:
                rsc = jload(fh)
        except:
            return 0

        length = 0

        # piCtory Config prüfen
        if "Devices" not in rsc:
            return 0

        # Letzes piCtory Device laden
        last_dev = rsc["Devices"].pop()
        length += last_dev["offset"]

        # bei mem beginnen, weil nur der höchste IO benötigt wird
        for type_iom in ["mem", "out", "inp"]:
            lst_iom = sorted(
                last_dev[type_iom],
                key=lambda x: int(x),
                reverse=True
            )

            if len(lst_iom) > 0:
                # Daten des letzen IOM auswerten
                last_iom = last_dev[type_iom][str(lst_iom[0])]
                bitlength = int(last_iom[2])
                length += int(last_iom[3])
                length += 1 if bitlength == 1 else int(bitlength / 8)
                break

        return length

    def _on_connect(self, client, userdata, flags, rc):
        """Verbindung zu MQTT Broker."""
        if rc > 0:
            self.__mqttend = True
            raise RuntimeError("can not connect to mqtt server")

        # Subscribe piCtory Anforderung
        client.subscribe(self._mqtt_sendpictory)

    def _on_message(self, client, userdata, msg):
        """Sendet piCtory Konfiguration."""

        # piCtory Konfiguration senden
        with open(proginit.pargs.configrsc, "rb") as fh:
            client.publish(self._mqtt_pictory, fh.read())

        # Prozessabbild senden
        self._evt_data.set()

    def newlogfile(self):
        """Konfiguriert die FileHandler auf neue Logdatei."""
        pass

    def run(self):
        """Startet die Uebertragung per MQTT."""
        proginit.logger.debug("enter MqttServer.start()")

        # Prozessabbild öffnen
        try:
            fh_proc = open(proginit.pargs.procimg, "r+b", 0)
        except:
            fh_proc = None
            self.__exit = True
            proginit.logger.error(
                "can not open process image {}".format(proginit.pargs.procimg)
            )

        # MQTT verbinden
        self._mq.connect(self._host, self._port, keepalive=60)
        self._mq.loop_start()

        # mainloop
        while not self.__exit:
            self._evt_data.clear()

            # Prozessabbild mit Daten übertragen
            self._mq.publish(
                self._mqtt_picontrol,
                fh_proc.read(self._procimglength)
            )
            fh_proc.seek(0)

            self._evt_data.wait(self._sendinterval)

        # MQTT trennen
        self._mq.loop_stop()
        self._mq.disconnect()

        # FileHandler schließen
        if fh_proc is not None:
            fh_proc.close()

        proginit.logger.debug("leave MqttServer.start()")

    def stop(self):
        """Stoppt die Uebertragung per MQTT."""
        proginit.logger.debug("enter MqttServer.stop()")
        self.__exit = True
        self._evt_data.set()
        proginit.logger.debug("leave MqttServer.stop()")
