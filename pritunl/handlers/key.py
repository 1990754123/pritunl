from pritunl.constants import *
from pritunl.exceptions import *
from pritunl.helpers import *
from pritunl import settings
from pritunl import utils
from pritunl import static
from pritunl import organization
from pritunl import settings
from pritunl import app
from pritunl import auth
from pritunl import mongo

import os
import flask
import uuid
import time
import random
import json
import base64
import re
import hashlib
import datetime
import hmac
import pymongo
import bson

def _get_key_archive(org_id, user_id):
    org = organization.get_by_id(org_id)
    user = org.get_user(user_id)
    key_archive = user.build_key_archive()
    response = flask.Response(response=key_archive,
        mimetype='application/octet-stream')
    response.headers.add('Content-Disposition',
        'attachment; filename="%s.tar"' % user.name)
    return response

@app.app.route('/key/<org_id>/<user_id>.tar', methods=['GET'])
@auth.session_auth
def user_key_archive_get(org_id, user_id):
    return _get_key_archive(org_id, user_id)

@app.app.route('/key/<org_id>/<user_id>', methods=['GET'])
@auth.session_auth
def user_key_link_get(org_id, user_id):
    org = organization.get_by_id(org_id)
    return utils.jsonify(org.create_user_key_link(user_id))

@app.app.route('/key/<key_id>.tar', methods=['GET'])
def user_linked_key_archive_get(key_id):
    utils.rand_sleep()

    collection = mongo.get_collection('users_key_link')
    doc = collection.find_one({
        'key_id': key_id,
    })

    if not doc:
        time.sleep(settings.app.rate_limit_sleep)
        return flask.abort(404)

    return _get_key_archive(doc['org_id'], doc['user_id'])

@app.app.route('/k/<short_id>', methods=['GET'])
def user_linked_key_page_get(short_id):
    utils.rand_sleep()

    collection = mongo.get_collection('users_key_link')
    doc = collection.find_one({
        'short_id': short_id,
    })

    if not doc:
        time.sleep(settings.app.rate_limit_sleep)
        return flask.abort(404)

    org = organization.get_by_id(doc['org_id'])
    user = org.get_user(id=doc['user_id'])

    key_page = static.StaticFile(settings.conf.www_path, KEY_VIEW_NAME,
        cache=False).data
    key_page = key_page.replace('<%= user_name %>', '%s - %s' % (
        org.name, user.name))
    key_page = key_page.replace('<%= user_key_url %>', '/key/%s.tar' % (
        doc['key_id']))

    if org.otp_auth:
        key_page = key_page.replace('<%= user_otp_key %>', user.otp_secret)
        key_page = key_page.replace('<%= user_otp_url %>',
            'otpauth://totp/%s@%s?secret=%s' % (
                user.name, org.name, user.otp_secret))
    else:
        key_page = key_page.replace('<%= user_otp_key %>', '')
        key_page = key_page.replace('<%= user_otp_url %>', '')

    key_page = key_page.replace('<%= short_id %>', doc['short_id'])

    conf_links = ''
    for server in org.iter_servers():
        conf_links += '<a class="btn btn-sm" title="Download Key" ' + \
            'href="/key/%s/%s.key">Download Key (%s)</a><br>\n' % (
                doc['key_id'], server.id, server.name)
    key_page = key_page.replace('<%= conf_links %>', conf_links)

    return key_page

@app.app.route('/k/<short_id>', methods=['DELETE'])
def user_linked_key_page_delete_get(short_id):
    utils.rand_sleep()

    collection = mongo.get_collection('users_key_link')
    collection.remove({
        'short_id': short_id,
    })
    return utils.jsonify({})

@app.app.route('/ku/<short_id>', methods=['GET'])
def user_uri_key_page_get(short_id):
    utils.rand_sleep()

    collection = mongo.get_collection('users_key_link')
    doc = collection.find_one({
        'short_id': short_id,
    })

    if not doc:
        time.sleep(settings.app.rate_limit_sleep)
        return flask.abort(404)

    org = organization.get_by_id(doc['org_id'])
    user = org.get_user(id=doc['user_id'])

    keys = {}
    for server in org.iter_servers():
        key = user.build_key_conf(server.id)
        keys[key['name']] = key['conf']

    return utils.jsonify(keys)

@app.app.route('/key/<key_id>/<server_id>.key', methods=['GET'])
def user_linked_key_conf_get(key_id, server_id):
    utils.rand_sleep()

    collection = mongo.get_collection('users_key_link')
    doc = collection.find_one({
        'key_id': key_id,
    })

    if not doc:
        time.sleep(settings.app.rate_limit_sleep)
        return flask.abort(404)

    org = organization.get_by_id(doc['org_id'])
    user = org.get_user(id=doc['user_id'])
    key_conf = user.build_key_conf(server_id)

    response = flask.Response(response=key_conf['conf'],
        mimetype='application/octet-stream')
    response.headers.add('Content-Disposition',
        'attachment; filename="%s"' % key_conf['name'])

    return response

@app.app.route('/key/<org_id>/<user_id>/<server_id>/<key_hash>',
    methods=['GET'])
def key_sync_get(org_id, user_id, server_id, key_hash):
    utils.rand_sleep()

    auth_token = flask.request.headers.get('Auth-Token', None)
    auth_timestamp = flask.request.headers.get('Auth-Timestamp', None)
    auth_nonce = flask.request.headers.get('Auth-Nonce', None)
    auth_signature = flask.request.headers.get('Auth-Signature', None)
    if not auth_token or not auth_timestamp or not auth_nonce or \
            not auth_signature:
        raise flask.abort(401)
    auth_nonce = auth_nonce[:32]

    try:
        if abs(int(auth_timestamp) - int(utils.time_now())) > \
                settings.app.auth_time_window:
            raise flask.abort(401)
    except ValueError:
        raise flask.abort(401)

    org = organization.get_by_id(org_id)
    if not org:
        raise flask.abort(401)

    user = org.get_user(id=user_id)
    if not user:
        raise flask.abort(401)
    elif not user.sync_secret:
        raise flask.abort(401)

    auth_string = '&'.join([
        auth_token, auth_timestamp, auth_nonce, flask.request.method,
        flask.request.path] +
        ([flask.request.data] if flask.request.data else []))

    if len(auth_string) > AUTH_SIG_STRING_MAX_LEN:
        raise flask.abort(401)

    auth_test_signature = base64.b64encode(hmac.new(
        user.sync_secret.encode(), auth_string,
        hashlib.sha256).digest())
    if auth_signature != auth_test_signature:
        raise flask.abort(401)

    nonces_collection = mongo.get_collection('auth_nonces')
    try:
        nonces_collection.insert({
            'token': auth_token,
            'nonce': auth_nonce,
            'timestamp': utils.now(),
        }, w=0)
    except pymongo.errors.DuplicateKeyError:
        raise flask.abort(401)

    key_conf = user.sync_conf(server_id, key_hash)
    if key_conf:
        return key_conf['conf']
    return ''
