# -*- coding: utf-8 -*-
#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
"""Helperfunktionen fuer das gesamte RevPiPyLoad-System."""
import os
import proginit
from re import match as rematch
from subprocess import Popen, PIPE


def _setuprt(pid, evt_exit):
    """Konfiguriert Programm fuer den RT-Scheduler.
    @param pid PID, der angehoben werden soll
    @return None"""
    if proginit.logger is not None:
        proginit.logger.debug("enter _setuprt()")

    dict_change = {
        "ksoftirqd/0,ksoftirqd/1,ksoftirqd/2,ksoftirqd/3": 10,
        "ktimersoftd/0,ktimersoftd/1,ktimersoftd/2,ktimersoftd/3": 20
    }

    for ps_change in dict_change:
        # pid und prio ermitteln
        kpidps = Popen([
            "/bin/ps", "-o", "pid=,rtprio=", "-C", ps_change
        ], bufsize=1, stdout=PIPE)

        # Timeout nachbilden da in Python 3.2 nicht vorhanden
        count = 10
        while kpidps.poll() is None:
            count -= 1
            if count == 0:
                kpidps.kill()
                if proginit.logger is not None:
                    proginit.logger.error(
                        "ps timeout to get rt prio info - no rt active"
                    )
                return None

            evt_exit.wait(0.5)
            if evt_exit.is_set():
                return None

        try:
            kpiddat = kpidps.communicate()[0]
            lst_kpids = kpiddat.split()
        except:
            kpidps.kill()
            if proginit.logger is not None:
                proginit.logger.error(
                    "can not get pid and prio - no rt active"
                )
            return None

        while len(lst_kpids) > 0:
            # Elemente paarweise übernehmen
            kpid = lst_kpids.pop(0)
            kprio = lst_kpids.pop(0)

            # Daten prüfen
            if not kpid.isdigit():
                if proginit.logger is not None:
                    proginit.logger.error(
                        "pid={} and prio={} are not valid - no rt active"
                        "".format(kpid, kprio)
                    )
                return None
            kpid = int(kpid)

            # RTPrio ermitteln
            if kprio.isdigit():
                kprio = int(kprio)
            else:
                kprio = 0

            if kprio < 10:
                # Profile anpassen
                ec = os.system("/usr/bin/env chrt -fp {} {}".format(
                    dict_change[ps_change], kpid
                ))
                if ec != 0:
                    if proginit.logger is not None:
                        proginit.logger.error(
                            "could not adjust scheduler - no rt active"
                        )
                    return None

    # SCHED_RR für pid setzen
    if proginit.logger is not None:
        proginit.logger.info("set scheduler profile of pid {}".format(pid))

    ec = os.system("/usr/bin/env chrt -p 1 {}".format(pid))
    if ec != 0 and proginit.logger is not None:
        proginit.logger.error(
            "could not set scheduler profile of pid {}"
            "".format(pid)
        )

    if proginit.logger is not None:
        proginit.logger.debug("leave _setuprt()")


def _zeroprocimg():
    """Setzt Prozessabbild auf NULL."""
    procimg = "/dev/piControl0" if proginit.pargs is None \
        else proginit.pargs.procimg

    if os.access(procimg, os.W_OK):
        with open(procimg, "w+b", 0) as f:
            f.write(bytes(4096))
    else:
        if proginit.logger is not None:
            proginit.logger.error(
                "zeroprocimg can not write to piControl device"
            )


def refullmatch(regex, string):
    """re.fullmatch wegen alter python version aus wheezy nachgebaut.

    @param regex RegEx Statement
    @param string Zeichenfolge gegen die getestet wird
    @return True, wenn komplett passt sonst False

    """
    m = rematch(regex, string)
    return m is not None and m.end() == len(string)
