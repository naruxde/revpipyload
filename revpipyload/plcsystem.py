# -*- coding: utf-8 -*-
#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
"""Modul fuer die Verwaltung der PLC Funktionen."""
import os
import proginit
import shlex
import subprocess
from logsystem import PipeLogwriter
from helper import _setuprt, _zeroprocimg
from sys import stdout as sysstdout
from threading import Event, Thread
from time import sleep, asctime


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

        self._arguments = arguments
        self._evt_exit = Event()
        self._plw = self._configureplw()
        self._program = program
        self._procplc = None
        self._pversion = pversion
        self.autoreload = False
        self.exitcode = None
        self.gid = 65534
        self.uid = 65534
        self.rtlevel = 0
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

        # LogWriter starten und Logausgaben schreiben
        if self._plw is not None:
            self._plw.logline("-" * 55)
            self._plw.logline("plc: {} started: {}".format(
                os.path.basename(self._program), asctime()
            ))
            self._plw.start()

        # Befehlstliste aufbauen
        lst_proc = shlex.split("/usr/bin/env {} -u {} {}".format(
            "python2" if self._pversion == 2 else "python3",
            self._program,
            self._arguments
        ))

        # Prozess erstellen
        proginit.logger.info("start plc program {}".format(self._program))
        self._procplc = self._spopen(lst_proc)

        # RealTime Scheduler nutzen nach 5 Sekunden Programmvorlauf
        if self.rtlevel > 0 \
                and not self._evt_exit.wait(5) \
                and self._procplc.poll() is None:
            _setuprt(self._procplc.pid, self._evt_exit)

        # Überwachung starten
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

            self._evt_exit.wait(0.5)

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
