from pritunl.server.output import ServerOutput
from pritunl.server.output_link import ServerOutputLink
from pritunl.server.bandwidth import ServerBandwidth
from pritunl.server.ip_pool import ServerIpPool
from pritunl.server.instance import ServerInstance
from pritunl.server.server import Server, dict_fields

from pritunl.constants import *
from pritunl.exceptions import *
from pritunl.helpers import *
from pritunl import utils
from pritunl import transaction
from pritunl import mongo
from pritunl import ipaddress

import uuid
import os
import signal
import time
import datetime
import subprocess
import threading
import traceback
import re
import json
import bson

def new_server(**kwargs):
    server = Server(**kwargs)
    server.initialize()
    return server

def get_by_id(id, fields=None):
    return Server(id=id, fields=fields)

def get_dict(id):
    return Server(id=id, fields=dict_fields).dict()

def get_used_resources(ignore_server_id):
    response = Server.collection.aggregate([
        {'$match': {
            '_id': {'$ne': ignore_server_id},
        }},
        {'$project': {
            'network': True,
            'interface': True,
            'port_protocol': {'$concat': [
                {'$substr': ['$port', 0, 5]},
                '$protocol',
            ]},
        }},
        {'$group': {
            '_id': None,
            'networks': {'$addToSet': '$network'},
            'interfaces': {'$addToSet': '$interface'},
            'ports': {'$addToSet': '$port_protocol'},
        }},
    ])

    used_resources = None
    for used_resources in response:
        break

    if used_resources:
        used_resources.pop('_id')
    else:
        used_resources = {
            'networks': set(),
            'interfaces': set(),
            'ports': set(),
        }

    return {
        'networks': {ipaddress.IPNetwork(
            x) for x in used_resources['networks']},
        'interfaces': set(used_resources['interfaces']),
        'ports': set(used_resources['ports']),
    }

def iter_servers(spec=None, fields=None):
    if fields:
        fields = {key: True for key in fields}

    for doc in Server.collection.find(spec or {}, fields).sort('name'):
        yield Server(doc=doc, fields=fields)

def iter_servers_dict():
    fields = {key: True for key in dict_fields}
    for doc in Server.collection.find({}, fields).sort('name'):
        yield Server(doc=doc, fields=fields).dict()

def output_get(server_id):
    return ServerOutput(server_id).get_output()

def output_clear(server_id):
    ServerOutput(server_id).clear_output()

def output_link_get(server_id):
    return ServerOutputLink(server_id).get_output()

def output_link_clear(server_id):
    svr = get_by_id(server_id, fields=['_id', 'links'])
    ServerOutputLink(server_id).clear_output(
        [x['server_id'] for x in svr.links])

def bandwidth_get(server_id, period):
    return ServerBandwidth(server_id).get_period(period)

def link_servers(server_id, link_server_id, use_local_address=False):
    if server_id == link_server_id:
        raise TypeError('Server id must be different then link server id')

    collection = mongo.get_collection('servers')

    count = 0
    spec = {
        '_id': {'$in': [server_id, link_server_id]},
    }
    project = {
        '_id': True,
        'status': True,
        'hosts': True,
        'replica_count': True
    }

    hosts = set()
    for doc in collection.find(spec, project):
        if doc['status'] == ONLINE:
            raise ServerLinkOnlineError('Server must be offline to link')

        if doc['replica_count'] > 1:
            raise ServerLinkReplicaError('Server has replicas')

        hosts_set = set(doc['hosts'])
        if hosts & hosts_set:
            raise ServerLinkCommonHostError('Servers have a common host')
        hosts.update(hosts_set)

        count += 1
    if count != 2:
        raise ServerLinkError('Link server not found')

    tran = transaction.Transaction()
    collection = tran.collection('servers')

    collection.update({
        '_id': server_id,
        'links.server_id': {'$ne': link_server_id},
    }, {'$push': {
        'links': {
            'server_id': link_server_id,
            'user_id': None,
            'use_local_address': use_local_address,
        },
    }})

    collection.update({
        '_id': link_server_id,
        'links.server_id': {'$ne': server_id},
    }, {'$addToSet': {
        'links': {
            'server_id': server_id,
            'user_id': None,
            'use_local_address': use_local_address,
        },
    }})

    tran.commit()

def unlink_servers(server_id, link_server_id):
    collection = mongo.get_collection('servers')

    count = 0
    spec = {
        '_id': {'$in': [server_id, link_server_id]},
    }
    project = {
        '_id': True,
        'status': True,
    }

    for doc in collection.find(spec, project):
        if doc['status'] == ONLINE:
            raise ServerLinkOnlineError('Server must be offline to unlink')

    tran = transaction.Transaction()
    collection = tran.collection('servers')

    collection.update({
        '_id': server_id,
    }, {'$pull': {
        'links': {'server_id': link_server_id},
    }})

    collection.update({
        '_id': link_server_id,
    }, {'$pull': {
        'links': {'server_id': server_id},
    }})

    tran.commit()
