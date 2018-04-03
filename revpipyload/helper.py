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


class IpAclManager():

    """Verwaltung fuer IP Adressen und deren ACL Level."""

    def __init__(self, minlevel, maxlevel, acl=None):
        """Init IpAclManager class.

        @param minlevel Smallest access level (min. 0)
        @param maxlevel Biggest access level (max. 9)
        @param acl ACL Liste fuer Berechtigungen als <class 'str'>

        """
        if type(minlevel) != int:
            raise ValueError("parameter minlevel must be <class 'int'>")
        if type(maxlevel) != int:
            raise ValueError("parameter maxlevel must be <class 'int'>")
        if minlevel < 0:
            raise ValueError("minlevel must be 0 or more")
        if maxlevel > 9:
            raise ValueError("maxlevel maximum is 9")
        if minlevel > maxlevel:
            raise ValueError("minlevel is smaller than maxlevel")

        self.__dict_acl = {}
        self.__dict_regex = {}
        self.__dict_knownips = {}
        self.__re_ipacl = "(([\\d\\*]{1,3}\\.){3}[\\d\\*]{1,3},[" \
            + str(minlevel) + "-" + str(maxlevel) + "] ?)*"

        # Liste erstellen, wenn übergeben
        if acl is not None:
            self.__set_acl(acl)

    def __iter__(self):
        """Gibt einzelne ACLs als <class 'tuple'> aus."""
        for aclip in sorted(self.__dict_acl):
            yield (aclip, self.__dict_acl[aclip])

    def __get_acl(self):
        """Getter fuer den rohen ACL-String.
        return ACLs als <class 'str'>"""
        str_acl = ""
        for aclip in sorted(self.__dict_acl):
            str_acl += "{},{} ".format(aclip, self.__dict_acl[aclip])
        return str_acl.strip()

    def __get_regex_acl(self):
        """Gibt formatierten RegEx-String zurueck.
        return RegEx Code als <class 'str'>"""
        return self.__re_ipacl

    def __set_acl(self, value):
        """Uebernimmt neue ACL-Liste fuer die Ausertung der Level.
        @param value Neue ACL-Liste als <class 'str'>"""
        if type(value) != str:
            raise ValueError("parameter acl must be <class 'str'>")

        value = value.strip()
        if not refullmatch(self.__re_ipacl, value):
            raise ValueError("acl format ist not okay - 1.2.3.4,0 5.6.7.8,1")

        # Klassenwerte übernehmen
        self.__dict_acl = {}
        self.__dict_regex = {}
        self.__dict_knownips = {}

        # Liste neu füllen mit regex Strings
        for ip_level in value.split():
            ip, level = ip_level.split(",", 1)
            self.__dict_acl[ip] = int(level)
            self.__dict_regex[ip] = \
                ip.replace(".", "\\.").replace("*", "\\d{1,3}")

    def get_acllevel(self, ipaddress):
        """Prueft IP gegen ACL List und gibt ACL-Wert aus.
        @param ipaddress zum pruefen
        @return <class 'int'> ACL Wert oder -1 wenn nicht gefunden"""
        # Bei bereits aufgelösten IPs direkt ACL auswerten
        if ipaddress in self.__dict_knownips:
            return self.__dict_knownips[ipaddress]

        for aclip in sorted(self.__dict_acl, reverse=True):
            if refullmatch(self.__dict_regex[aclip], ipaddress):
                # IP und Level merken
                self.__dict_knownips[ipaddress] = self.__dict_acl[aclip]

                # Level zurückgeben
                return self.__dict_acl[aclip]

        return -1

    def loadacl(self, str_acl):
        """Laed ACL String und gibt erfolg zurueck.
        @param str_acl ACL als <class 'str'>
        @return True, wenn erfolgreich uebernommen"""
        if not refullmatch(self.__re_ipacl, str_acl):
            return False
        self.__set_acl(str_acl)
        return True

    acl = property(__get_acl, __set_acl)
    regex_acl = property(__get_regex_acl)


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
