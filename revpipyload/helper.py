# -*- coding: utf-8 -*-
"""Helperfunktionen fuer das gesamte RevPiPyLoad-System."""
__author__ = "Sven Sager"
__copyright__ = "Copyright (C) 2018 Sven Sager"
__license__ = "GPLv3"
import os
import proginit
from configparser import ConfigParser
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
        except Exception:
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
                        "pid={0} and prio={1} are not valid - no rt active"
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
                ec = os.system("/usr/bin/env chrt -fp {0} {1}".format(
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
        proginit.logger.info("set scheduler profile of pid {0}".format(pid))

    ec = os.system("/usr/bin/env chrt -p 1 {0}".format(pid))
    if ec != 0 and proginit.logger is not None:
        proginit.logger.error(
            "could not set scheduler profile of pid {0}"
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


def revpimodio_replaceio(revpi, filename):
    """Importiert und ersetzt IOs in RevPiModIO.

    @param revpi RevPiModIO Instanz
    @param filename Dateiname der Ersetzungsdatei
    @return True, wenn alle IOs ersetzt werden konnten

    """
    cp = ConfigParser()
    try:
        with open(filename, "r") as fh:
            cp.read_file(fh)
    except Exception as e:
        proginit.logger.error(
            "could not read replace_io file '{0}' | {1}".format(filename, e)
        )
        return False

    # Pre-check
    lst_replace = []
    rc = True
    for io in cp:
        if io == "DEFAULT":
            continue

        dict_replace = {
            "replace": cp[io].get("replace", ""),
            "frm": cp[io].get("frm"),
            "bmk": cp[io].get("bmk", ""),
            "byteorder": cp[io].get("byteorder", "little"),
        }

        if dict_replace["replace"] in revpi.io:

            # Byteorder prüfen
            if not (dict_replace["byteorder"] == "little" or
                    dict_replace["byteorder"] == "big"):
                proginit.logger.error(
                    "byteorder of '{0}' must be 'little' or 'big'".format(io)
                )
                rc = False
                continue

            if dict_replace["frm"] == "?":

                # Convert defaultvalue from config file
                try:
                    dict_replace["default"] = cp[io].getboolean("defaultvalue")
                except Exception:
                    proginit.logger.error(
                        "could not convert '{0}' defaultvalue '{1}' to boolean"
                        "".format(io, cp[io].get("defaultvalue"))
                    )
                    rc = False
                    continue

                # Get bitaddress
                try:
                    dict_replace["bitaddress"] = cp[io].getint("bitaddress", 0)
                except Exception:
                    proginit.logger.error(
                        "could not convert '{0}' bitaddress '{1}' to integer"
                        "".format(io, cp[io].get("bitaddress"))
                    )
                    rc = False
                    continue

            else:
                # Convert defaultvalue from config file
                try:
                    dict_replace["default"] = cp[io].getint("defaultvalue")
                except Exception:
                    proginit.logger.error(
                        "could not convert '{0}' defaultvalue '{1}' to integer"
                        "".format(io, cp[io].get("defaultvalue"))
                    )
                    rc = False
                    continue

        else:
            proginit.logger.error(
                "can not find io '{0}' to replace with '{1}'"
                "".format(dict_replace["replace"], io)
            )
            rc = False
            continue

        # Replace_IO übernehmen
        lst_replace.append(dict_replace)

    if not rc:
        # Abbrechen, wenn IO-Verarbeitung einen Fehler hatte
        return False

    # Replace IOs
    for dict_replace in lst_replace:

        # FIXME: Hier können Fehler auftreten !!!

        revpi.io[dict_replace["replace"]].replace_io(
            io,
            frm=dict_replace["frm"],
            bmk=dict_replace["bmk"],
            bit=dict_replace["bitaddress"],
            byteorder=dict_replace["byteorder"],
            defaultvalue=dict_replace["default"]
        )


def refullmatch(regex, string):
    """re.fullmatch wegen alter python version aus wheezy nachgebaut.

    @param regex RegEx Statement
    @param string Zeichenfolge gegen die getestet wird
    @return True, wenn komplett passt sonst False

    """
    m = rematch(regex, string)
    return m is not None and m.end() == len(string)
