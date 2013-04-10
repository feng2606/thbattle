# -*- coding: utf-8 -*-
# -- stdlib --
import sys
import argparse
from urllib import unquote
import atexit
from collections import deque
import time

# -- third party --
import gevent
from gevent import monkey
monkey.patch_all()

from gevent.event import Event
from gevent import Timeout, Greenlet
from bottle import route, run, request, response
import pika
import simplejson as json

# -- own --
from utils import instantiate, swallow, surpress_and_restart
from utils.rpc import RPCClient
from network import Endpoint


# -- code --
parser = argparse.ArgumentParser(sys.argv[0])
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=7001)
parser.add_argument('--discuz-cookiepre', default='VfKd_')
options = parser.parse_args()


member_service = RPCClient(('127.0.0.1', 7000), timeout=2)
current_users = {}
event_waiters = set()
events_history = deque([[None, 0]] * 100)


@instantiate
class Interconnect(object):
    def __init__(self):
        self.conn = conn = pika.BlockingConnection()
        self.chan = chan = conn.channel()
        chan.exchange_declare('thb_events', 'fanout')

    def publish(self, key, body):
        self.chan.basic_publish(
            'thb_events', '%s:%s' % ('forum', key),
            Endpoint.encode(body),
        )

    def shutdown(self):
        swallow(self.chan.close)()
        swallow(self.conn.close)()

atexit.register(Interconnect.shutdown)


class InterconnectHandler(Greenlet):
    @surpress_and_restart
    def _run(self):
        try:
            conn = pika.BlockingConnection()
            chan = conn.channel()
            chan.exchange_declare('thb_events', 'fanout')
            queue = chan.queue_declare(
                exclusive=True,
                auto_delete=True,
                arguments={'x-message-ttl': 1000},
            )
            chan.queue_bind(queue.method.queue, 'thb_events')

            def notify(key, message):
                def _notify():
                    events_history.rotate()
                    events_history[0] = [[key, message], time.time()]
                    encoded = json.dumps([key, message])
                    [evt.set() for evt in list(event_waiters)]

                return gevent.spawn(_notify)

            for method, header, body in chan.consume(queue.method.queue):
                chan.basic_ack(method.delivery_tag)
                message = json.loads(body)
                key = method.routing_key
                node, topic = key.split(':')

                if topic == 'current_users':
                    # [[node, username, state], ...]
                    current_users[node] = [
                        (node, i[1], i[2]) for i in message
                    ]
                    rst = []
                    map(rst.__iadd__, current_users.values())
                    notify('current_users', rst)

                elif topic == 'shutdown':
                    # not implemented yet
                    current_users[node] = []
                    rst = []
                    map(rst.__iadd__, current_users.values())
                    notify('current_users', rst)

                if topic == 'speaker':
                    # [node, username, content]
                    message.insert(0, node)
                    notify('speaker', message)

        finally:
            swallow(chan.close)()
            swallow(conn.close)()
            gevent.sleep(1)

    def __repr__(self):
        return self.__class__.__name__

InterconnectHandler = InterconnectHandler.spawn()


@route('/interconnect/onlineusers')
def onlineusers():
    rst = []
    map(rst.__iadd__, current_users.values())
    return json.dumps(rst)


@route('/interconnect/events')
def events():
    try:
        last = float(request.get_cookie('interconnect_last_event'))
    except:
        last = time.time()

    evt = Event()

    events_history[0][1] > last and evt.set()

    event_waiters.add(evt)
    evt.wait(timeout=30)
    event_waiters.discard(evt)

    response.set_header('Content-Type', 'application/json')
    response.set_header('Cache-Control', 'no-cache')
    response.set_cookie('interconnect_last_event', '%.5f' % time.time())

    data = []
    for e in events_history:
        if e[1] > last:
            data.append(e[0])
        else:
            break

    data = list(reversed(data))
    return json.dumps(data)


@route('/interconnect/speaker', method='POST')
def speaker():
    idx = {
        k.split('_')[-1]: k for k in request.cookies
        if k.startswith(options.discuz_cookiepre)
    }

    if not ('auth' in idx and 'saltkey' in idx):
        response.status = 403
        return

    auth = unquote(request.get_cookie(idx['auth']))
    saltkey = unquote(request.get_cookie(idx['saltkey']))
    member = member_service.validate_by_cookie(auth, saltkey)
    if not member:
        return 'false'

    if member['credits'] < 10:
        return 'false'

    message = request.forms.get('message').decode('utf-8', 'ignore')
    username = member['username'].decode('utf-8', 'ignore')
    member_service.add_credit(member['uid'], 'credits', -10)

    Interconnect.publish('speaker', [username, message])

    return 'true'


run(server='gevent', host=options.host, port=options.port)
