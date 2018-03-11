# -*- coding: utf-8 -*-
#
# RevPiPyLoad
#
# Webpage: https://revpimodio.org/revpipyplc/
# (c) Sven Sager, License: LGPLv3
#
"""XML-RPC Server anpassungen fuer Absicherung."""
import proginit
from helper import IpAclManager
from concurrent import futures
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler


class SaveXMLRPCServer(SimpleXMLRPCServer):

    """Erstellt einen erweiterten XMLRPCServer."""

    def __init__(
            self, addr, logRequests=True, allow_none=False,
            use_builtin_types=False, ipacl=IpAclManager()):
        """Init SaveXMLRPCServer class."""
        proginit.logger.debug("enter SaveXMLRPCServer.__init__()")

        # Vererbte Klasse instantiieren
        super().__init__(
            addr=addr,
            requestHandler=SaveXMLRPCRequestHandler,
            logRequests=logRequests,
            allow_none=allow_none,
            encoding="utf-8",
            bind_and_activate=False,
            use_builtin_types=use_builtin_types
        )

        # Klassenvariablen
        self.aclmgr = ipacl
        self.tpe = futures.ThreadPoolExecutor(max_workers=1)
        self.fut = None

        proginit.logger.debug("leave SaveXMLRPCServer.__init__()")

    def isAlive(self):
        """Prueft ob der XML RPC Server laeuft.
        @return True, wenn Server noch laeuft"""
        return False if self.fut is None else self.fut.running()

    def start(self):
        """Startet den XML-RPC Server."""
        proginit.logger.debug("enter SaveXMLRPCServer.start()")

        if self.fut is None:
            self.server_bind()
            self.server_activate()
            self.fut = self.tpe.submit(self.serve_forever)
        else:
            raise RuntimeError("savexmlrpcservers can only be started once")

        proginit.logger.debug("leave SaveXMLRPCServer.start()")

    def stop(self):
        """Stoppt den XML-RPC Server."""
        proginit.logger.debug("enter SaveXMLRPCServer.stop()")

        if self.fut is not None:
            self.shutdown()
            self.tpe.shutdown()
            self.server_close()
        else:
            raise RuntimeError("save xml rpc server was not started")

        proginit.logger.debug("leave SaveXMLRPCServer.stop()")


class SaveXMLRPCRequestHandler(SimpleXMLRPCRequestHandler):

    """Verwaltet die XML-Requests und prueft Berechtigungen."""

    def parse_request(self):
        """Berechtigungen pruefen.
        @return True, wenn Parsen erfolgreich war"""
        # Request parsen und ggf. schon abbrechen
        if not super().parse_request():
            return False

        # IP-Adresse prüfen
        int_acl = self.server.aclmgr.get_acllevel(self.address_string())
        if int_acl >= 0:
            return True
        else:
            self.send_error(
                401,
                "IP '{}' not allowed with acl level '{}'"
                "".format(self.address_string(), int_acl)
            )

        return False
