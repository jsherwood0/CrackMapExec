#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Majorly stolen from https://gist.github.com/ropnop/7a41da7aabb8455d0898db362335e139
# Which in turn stole from Impacket :)
# Code refactored and added to by @mjhallenbeck (Marshall-Hallenbeck on GitHub)

import sys
import logging
import argparse

import impacket
from impacket.dcerpc.v5 import transport, lsat, lsad, samr
from impacket.dcerpc.v5.dtypes import MAXIMUM_ALLOWED


class SamrFunc:
    def __init__(self, connection):
        self.logger = connection.logger
        self.addr = connection.host if not connection.kerberos else connection.hostname + '.' + connection.domain
        self.protocol = connection.args.port
        self.username = connection.username
        self.password = connection.password
        self.domain = connection.domain
        self.hash = connection.hash
        self.lmhash = ''
        self.nthash = ''
        self.aesKey = None,
        self.doKerberos = connection.kerberos

        if self.hash is not None:
            if self.hash.find(':') != -1:
                self.lmhash, self.nthash = self.hash.split(':')
            else:
                self.nthash = self.hash

        if self.password is None:
            self.password = ''

        self.samr_query = SAMRQuery(
            username=self.username,
            password=self.password,
            remote_name=self.addr,
            remote_host=self.addr,
            kerberos=self.doKerberos
        )
        self.lsa_query = LSAQuery(
            username=self.username,
            password=self.password,
            remote_name=self.addr,
            remote_host=self.addr,
            kerberos=self.doKerberos
        )

    def get_builtin_groups(self):
        domains = self.samr_query.get_domains()

        if "Builtin" not in domains:
            logging.error(f"No Builtin group to query locally on")
            return

        domain_handle = self.samr_query.get_domain_handle('Builtin')
        groups = self.samr_query.get_domain_aliases(domain_handle)

        return groups

    def get_custom_groups(self):
        domains = self.samr_query.get_domains()
        custom_groups = {}

        for domain in domains:
            if domain == "Builtin":
                continue
            domain_handle = self.samr_query.get_domain_handle(domain)
            custom_groups.update(self.samr_query.get_domain_aliases(domain_handle))

        print(f"Local Groups: {custom_groups}")
        return custom_groups

    def get_local_groups(self):
        builtin_groups = self.get_builtin_groups()
        custom_groups = self.get_custom_groups()
        return {**builtin_groups, **custom_groups}

    def get_local_users(self):
        pass

    def get_local_administrators(self):
        self.get_builtin_groups()
        if "Administrators" in self.groups:
            self.logger.success(f"Found Local Administrators group: RID {self.groups['Administrators']}")
        domain_handle = self.samr_query.get_domain_handle('Builtin')
        self.logger.debug(f"Querying group members")
        member_sids = self.samr_query.get_alias_members(domain_handle, self.groups['Administrators'])
        member_names = self.lsa_query.lookup_sids(member_sids)

        for sid, name in zip(member_sids, member_names):
            print(f"{name} - {sid}")


class SAMRQuery:
    def __init__(self, username='', password='', domain='', port=445, remote_name='', remote_host='', kerberos=None):
        self.__username = username
        self.__password = password
        self.__domain = domain
        self.__lmhash = ''
        self.__nthash = ''
        self.__aesKey = None
        self.__port = port
        self.__remote_name = remote_name
        self.__remote_host = remote_host
        self.__kerberos = kerberos
        self.dce = self.get_dce()
        self.server_handle = self.get_server_handle()

    def get_transport(self):
        string_binding = f"ncacn_np:{self.__port}[\pipe\samr]"
        logging.debug(f"Binding to {string_binding}")
        # using a direct SMBTransport instead of DCERPCTransportFactory since we need the filename to be '\samr'
        rpc_transport = transport.SMBTransport(
            self.__remote_host,
            self.__port,
            r'\samr',
            self.__username,
            self.__password,
            self.__domain,
            self.__lmhash,
            self.__nthash,
            self.__aesKey,
            doKerberos=self.__kerberos
        )
        return rpc_transport

    def get_dce(self):
        rpc_transport = self.get_transport()
        try:
            dce = rpc_transport.get_dce_rpc()
            dce.connect()
            dce.bind(samr.MSRPC_UUID_SAMR)
        except impacket.nmb.NetBIOSError as e:
            logging.error(f"NetBIOSError on Connection: {e}")
            return
        except impacket.smbconnection.SessionError as e:
            logging.error(f"SessionError on Connection: {e}")
            return
        return dce

    def get_server_handle(self):
        if self.dce:
            try:
                resp = samr.hSamrConnect(self.dce)
            except samr.DCERPCException as e:
                logging.debug(f"Error while connecting with Samr: {e}")
                return None
            return resp['ServerHandle']
        else:
            logging.debug(f"Error creating Samr handle")
            return

    def get_domains(self):
        resp = samr.hSamrEnumerateDomainsInSamServer(self.dce, self.server_handle)
        domains = resp['Buffer']['Buffer']
        domain_names = []
        for domain in domains:
            domain_names.append(domain['Name'])
        return domain_names

    def get_domain_handle(self, domain_name):
        resp = samr.hSamrLookupDomainInSamServer(self.dce, self.server_handle, domain_name)
        resp = samr.hSamrOpenDomain(self.dce, serverHandle=self.server_handle, domainId=resp['DomainId'])
        return resp['DomainHandle']

    def get_domain_aliases(self, domain_handle):
        resp = samr.hSamrEnumerateAliasesInDomain(self.dce, domain_handle)
        aliases = {}
        for alias in resp['Buffer']['Buffer']:
            aliases[alias['Name']] = alias['RelativeId']
        return aliases

    def get_alias_handle(self, domain_handle, alias_id):
        resp = samr.hSamrOpenAlias(self.dce, domain_handle, desiredAccess=MAXIMUM_ALLOWED, aliasId=alias_id)
        return resp['AliasHandle']

    def get_alias_members(self, domain_handle, alias_id):
        alias_handle = self.get_alias_handle(domain_handle, alias_id)
        resp = samr.hSamrGetMembersInAlias(self.dce, alias_handle)
        member_sids = []
        for member in resp['Members']['Sids']:
            member_sids.append(member['SidPointer'].formatCanonical())
        return member_sids


class LSAQuery:
    def __init__(self, username='', password='', domain='', port=445, remote_name='', remote_host='', kerberos=None):
        self.__username = username
        self.__password = password
        self.__domain = domain
        self.__lmhash = ''
        self.__nthash = ''
        self.__aesKey = None
        self.__port = port
        self.__remote_name = remote_name
        self.__remote_host = remote_host
        self.__kerberos = kerberos
        self.dce = self.get_dce()
        self.policy_handle = self.get_policy_handle()

    def get_transport(self):
        string_binding = f"ncacn_np:{self.__remote_name}[\\pipe\\lsarpc]"
        rpc_transport = transport.DCERPCTransportFactory(string_binding)
        rpc_transport.set_dport(self.__port)
        rpc_transport.setRemoteHost(self.__remote_host)
        if self.kerberos:
            rpc_transport.set_kerberos(True, self.__domain)
        if hasattr(rpc_transport, 'set_credentials'):
            # This method exists only for selected protocol sequences.
            rpc_transport.set_credentials(
                self.__username,
                self.__password,
                self.__domain,
                self.__lmhash,
                self.__nthash,
                self.__aesKey,
            )
        return rpc_transport

    def get_dce(self):
        rpc_transport = self.get_transport()
        try:
            dce = rpc_transport.get_dce_rpc()
            dce.connect()
            dce.bind(lsat.MSRPC_UUID_LSAT)
        except impacket.nmb.NetBIOSError as e:
            self.logger.error(f"NetBIOSError on Connection: {e}")
            return None
        return dce

    def get_policy_handle(self):
        resp = lsad.hLsarOpenPolicy2(self.dce, MAXIMUM_ALLOWED | lsat.POLICY_LOOKUP_NAMES)
        return resp['PolicyHandle']

    def lookup_sids(self, sids):
        resp = lsat.hLsarLookupSids(self.dce, self.policy_handle, sids, lsat.LSAP_LOOKUP_LEVEL.LsapLookupWksta)
        names = []
        for translated_names in resp['TranslatedNames']['Names']:
            names.append(translated_names['Name'])
        return names


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target', action='store', help='[[domain/]username[:password]@]<targetName or address>')

    group = parser.add_argument_group('authentication')
    group.add_argument('-no-pass', action='store_true', help='don\'t ask for password')

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    options = parser.parse_args()

    import re

    domain, username, password, remote_name = re.compile('(?:(?:([^/@:]*)/)?([^@:]*)(?::([^@]*))?@)?(.*)').match(
        options.target).groups('')

    # In case the password contains '@'
    if '@' in remote_name:
        password = password + '@' + remote_name.rpartition('@')[0]
        remote_name = remote_name.rpartition('@')[2]

    if domain is None:
        domain = ''

    if password == '' and username != '' and options.no_pass is False:
        from getpass import getpass

        password = getpass("Password:")

    samr_func = SamrFunc(
        username=username,
        password=password,
        remote_name=remote_name,
        remot_host=remote_name
    )
    builtin_groups = samr_func.get_builtin_groups()
    print(f"Built In Groups: {builtin_groups}")
