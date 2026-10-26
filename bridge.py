#!/usr/bin/env python3
"""A LAN-only UPnP MediaRenderer adapter for a Sonos speaker. Python 3.9+."""
import argparse
import collections
import http.client
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import random
import re
import os
import signal
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

ET.register_namespace('', 'urn:schemas-upnp-org:service-1-0')
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
EVENT = 'urn:schemas-upnp-org:event-1-0'
DEVICE = 'urn:schemas-upnp-org:device:MediaRenderer:1'
SERVICES = {
    'AVTransport': {'SetAVTransportURI', 'SetNextAVTransportURI', 'GetMediaInfo', 'GetTransportInfo', 'GetPositionInfo', 'GetDeviceCapabilities', 'GetTransportSettings', 'Stop', 'Play', 'Pause', 'Seek', 'Next', 'Previous', 'SetPlayMode', 'GetCurrentTransportActions'},
    'RenderingControl': {'GetMute', 'SetMute', 'GetVolume', 'SetVolume', 'GetVolumeDB', 'SetVolumeDB', 'GetVolumeDBRange', 'GetLoudness', 'SetLoudness'},
    'ConnectionManager': {'GetProtocolInfo', 'GetCurrentConnectionIDs', 'GetCurrentConnectionInfo'},
}
LOG = logging.getLogger('sonos-dlna')
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DTD is not supported')
    return ET.fromstring(data)

def local_ip(target):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((target, 1400))
        return s.getsockname()[0]

def stype(service):
    return 'urn:schemas-upnp-org:service:' + service + ':1'

def soap(service, action, args):
    body = ''.join('<%s>%s</%s>' % (k, escape(str(v)), k) for k, v in args.items())
    return ('<?xml version="1.0"?><s:Envelope xmlns:s="%s" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>' % (SOAP, action, stype(service), body, action)).encode()

def media_addresses(host, port, owner):
    ips = list(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    for ip in ips:
        address = ipaddress.ip_address(ip)
        # NetEase CDN domains may resolve into the proxy's synthetic-IP pool.
        # Keep the exception restricted to NetEase audio hosts and RFC 2544.
        fake_netease = (address.version == 4 and
            address in ipaddress.ip_network('198.18.0.0/15') and
            host.lower().endswith('.music.126.net'))
        if not address.is_global and ip != owner and not fake_netease:
            raise ValueError('Restricted media address')
    return ips

def media_duration(data):
    """Read finite track duration from MP4 movie headers or FLAC STREAMINFO."""
    if data.startswith(b'fLaC') and len(data) >= 42:
        block = data[8:42]
        packed = int.from_bytes(block[10:18], 'big')
        rate, samples = packed >> 44, packed & ((1 << 36) - 1)
        if rate and samples: return samples / rate
    def boxes(start, end):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack_from('>I4s', data, pos)
            header = 8
            if size == 1:
                if pos + 16 > end: return
                size = struct.unpack_from('>Q', data, pos + 8)[0]; header = 16
            if size < header: return
            if kind == b'moov':
                value = boxes(pos + header, min(pos + size, end))
                if value: return value
            if kind == b'mvhd' and pos + size <= end:
                payload = data[pos + header:pos + size]
                if not payload or payload[0] not in (0, 1): return
                off = 20 if payload[0] == 1 else 12
                width = 8 if payload[0] == 1 else 4
                if len(payload) < off + 4 + width: return
                scale = int.from_bytes(payload[off:off + 4], 'big')
                duration = int.from_bytes(payload[off + 4:off + 4 + width], 'big')
                if scale and duration and duration != (1 << (width * 8)) - 1:
                    return duration / scale
            pos += size
    return boxes(0, len(data))

def upnp_time(seconds):
    seconds = int(seconds)
    return '%02d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)

class Bridge:
    def __init__(self, target, host, port, name):
        self.target, self.host, self.port, self.name = target, host, port, name
        self.base = 'http://%s:%d' % (host, port)
        self.lock = threading.RLock()
        self.transport_lock = threading.RLock()
        self.transport_changed = threading.Condition()
        self.transport_pending = 0
        self.last_set_uri = 0
        self.stop = threading.Event()
        self.event_wake = threading.Event()
        self.transport_revision = 0
        self.media = collections.OrderedDict()
        self.state_path = None
        self.durations = {}
        self.duration_tasks = {}
        self.subs = {}
        self.stats = collections.Counter()
        self.last_action = None
        self.last_error = None
        self.controls, self.scpds = {}, {}
        source = 'http://%s:1400' % target
        with OPENER.open(source + '/xml/device_description.xml', timeout=5) as res:
            desc = xml(res.read(512000))
        # Only use the renderer's services (the server has a different ConnectionManager).
        renderer = next(d for d in desc.iter() if d.tag.endswith('device') and d.find('{*}deviceType') is not None and d.find('{*}deviceType').text == DEVICE)
        for srv in renderer.findall('{*}serviceList/{*}service'):
            fields = {e.tag.split('}')[-1]: e.text for e in srv}
            kind = fields['serviceType'].split(':')[-2]
            if kind not in SERVICES:
                continue
            self.controls[kind] = source + fields['controlURL']
            with OPENER.open(source + fields['SCPDURL'], timeout=5) as res:
                scpd = xml(res.read(512000))
            actions = scpd.find('{*}actionList')
            for action in list(actions):
                if action.find('{*}name').text not in SERVICES[kind]:
                    actions.remove(action)
            variables = scpd.find('{*}serviceStateTable')
            referenced = {n.text for n in actions.findall('.//{*}relatedStateVariable')}
            for variable in list(variables):
                if variable.find('{*}name').text not in referenced and variable.attrib.get('sendEvents') != 'yes':
                    variables.remove(variable)
            scpd.remove(actions); scpd.remove(variables)
            scpd.append(actions); scpd.append(variables)
            self.scpds[kind] = ET.tostring(scpd, encoding='utf-8', xml_declaration=True)
        if set(self.controls) != set(SERVICES):
            raise ValueError('Target does not offer all renderer services')
        udn = renderer.find('{*}UDN').text
        self.udn = 'uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'sonos-dlna-bridge:v2:' + udn))
        self.targets = [self.udn, 'upnp:rootdevice', DEVICE] + [stype(s) for s in SERVICES]
        sl = ''.join('<service><serviceType>%s</serviceType><serviceId>urn:upnp-org:serviceId:%s</serviceId><SCPDURL>/%s.xml</SCPDURL><controlURL>/control/%s</controlURL><eventSubURL>/event/%s</eventSubURL></service>' % (stype(s), s, s, s, s) for s in SERVICES)
        self.description = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion><URLBase>%s/</URLBase><device><deviceType>%s</deviceType><friendlyName>%s</friendlyName><manufacturer>Local Audio Bridge</manufacturer><modelDescription>UPnP audio bridge for Sonos</modelDescription><modelName>Audio Renderer</modelName><modelNumber>1</modelNumber><UDN>%s</UDN><dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC><serviceList>%s</serviceList></device></root>' % (self.base, DEVICE, escape(name), self.udn, sl)).encode()

    def call(self, service, action, args):
        req = urllib.request.Request(self.controls[service], data=soap(service, action, args), headers={'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"%s#%s"' % (stype(service), action)})
        try:
            with OPENER.open(req, timeout=6) as res:
                return res.status, res.read(1024 * 1024)
        except urllib.error.HTTPError as e:
            return e.code, e.read(1024 * 1024)

    def duration(self, path, wait=True):
        with self.lock:
            if path not in self.media: return None
            if path in self.durations: return self.durations[path]
            event = self.duration_tasks.get(path)
            if event is None:
                event = threading.Event(); self.duration_tasks[path] = event
                def probe():
                    try:
                        req = urllib.request.Request(self.base + path, headers={'Range': 'bytes=0-262143'})
                        with OPENER.open(req, timeout=5) as res:
                            value = media_duration(res.read(262144))
                        with self.lock: self.durations[path] = value
                        if value: LOG.info('Track duration detected: %.3f seconds', value)
                    except Exception as e:
                        LOG.info('Track duration unavailable: %s', type(e).__name__)
                        with self.lock: self.durations[path] = None
                    finally: event.set()
                threading.Thread(target=probe, daemon=True).start()
        if wait: event.wait(2)
        with self.lock: return self.durations.get(path)

    def normalize_response(self, service, action, data):
        if service != 'AVTransport' or action not in ('GetPositionInfo', 'GetMediaInfo'): return data
        root = xml(data); response = root.find('{%s}Body' % SOAP)[0]
        fields = {e.tag.split('}')[-1]: e for e in response}
        uri = fields.get('TrackURI' if action == 'GetPositionInfo' else 'CurrentURI')
        if uri is None or not (uri.text or '').startswith(self.base + '/media/'): return data
        duration = self.duration(urllib.parse.urlsplit(uri.text).path)
        if duration:
            key = 'TrackDuration' if action == 'GetPositionInfo' else 'MediaDuration'
            if key in fields and fields[key].text in ('0:00:00', '00:00:00', 'NOT_IMPLEMENTED', '', None):
                fields[key].text = upnp_time(duration)
        if action == 'GetPositionInfo':
            if 'AbsTime' in fields and fields['AbsTime'].text == 'NOT_IMPLEMENTED' and 'RelTime' in fields:
                fields['AbsTime'].text = fields['RelTime'].text
            for key in ('RelTime', 'AbsTime', 'TrackDuration'):
                if key in fields and re.fullmatch(r'\d+:\d{2}:\d{2}', fields[key].text or ''):
                    fields[key].text = fields[key].text.zfill(8)
        # A conventional SOAP envelope keeps response child names unprefixed.
        return soap(service, action + 'Response', {k: e.text or '' for k, e in fields.items()})

    def client_call(self, service, action, args):
        if service != 'AVTransport': return self._client_call(service, action, args)
        mutation = not action.startswith('Get')
        if mutation:
            with self.transport_changed:
                self.transport_pending += 1
                self.transport_changed.notify_all()
            try:
                with self.transport_lock:
                    return self._client_call(service, action, args)
            finally:
                with self.transport_changed:
                    self.transport_revision += 1
                    self.transport_pending -= 1
                    self.transport_changed.notify_all()
        # State readbacks must observe completed control operations, not their intermediate states.
        with self.transport_changed:
            remaining = 0.3 - (time.monotonic() - self.last_set_uri)
            if remaining > 0 and not self.transport_pending:
                self.transport_changed.wait_for(lambda: self.transport_pending > 0, timeout=remaining)
            self.transport_changed.wait_for(lambda: self.transport_pending == 0, timeout=5)
        with self.transport_lock:
            return self._client_call(service, action, args)

    def _client_call(self, service, action, args):
        status, data = self.call(service, action, args)
        if status == 200 and service == 'AVTransport':
            if action == 'SetAVTransportURI': self.last_set_uri = time.monotonic()
            if action in ('Play', 'Pause', 'GetTransportInfo'):
                deadline = time.monotonic() + 2
                state_data = data if action == 'GetTransportInfo' else None
                while time.monotonic() < deadline:
                    if state_data is None:
                        state_status, state_data = self.call('AVTransport', 'GetTransportInfo', {'InstanceID': 0})
                        if state_status != 200: break
                    state = xml(state_data).find('.//CurrentTransportState')
                    expected = {'Play': 'PLAYING', 'Pause': 'PAUSED_PLAYBACK'}.get(action)
                    if state is None: break
                    if expected and state.text == expected: break
                    if not expected and state.text != 'TRANSITIONING': break
                    time.sleep(0.1); state_data = None
                if action == 'GetTransportInfo' and state_data is not None: data = state_data
            data = self.normalize_response(service, action, data)
        return status, data

    def values(self, service, action, args):
        status, data = self.call(service, action, args)
        if status != 200:
            raise RuntimeError('Sonos rejected ' + action)
        data = self.normalize_response(service, action, data)
        response = xml(data).find('{%s}Body' % SOAP)[0]
        return {e.tag.split('}')[-1]: e.text or '' for e in response}

    def media_url(self, url, peer):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError('Only HTTP(S) media URLs are supported')
        token = uuid.uuid4().hex
        # Give clients/players a useful suffix; never expose source URLs in logs.
        suffix = Path(u.path).suffix.lower()
        if suffix not in ('.mp3', '.flac', '.m4a', '.aac', '.wav', '.ogg'):
            suffix = ''
        path = '/media/' + token + suffix
        with self.lock:
            self.media[path] = (url, peer)
            while len(self.media) > 128:
                self.media.popitem(last=False)
            if self.state_path:
                tmp = self.state_path.with_suffix('.tmp')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as f: json.dump(self.media, f)
                tmp.replace(self.state_path)
        self.duration(path, wait=False)
        return self.base + path

    def restore(self, data):
        with self.lock:
            entries = list(self.media.items())
        for path, (original, _) in entries:
            data = data.replace(escape(self.base + path).encode(), escape(original).encode())
        return data

    def status(self):
        with self.lock:
            return {'name': self.name, 'target': self.target, 'url': self.base,
                    'subscriptions': len(self.subs), 'stats': dict(self.stats),
                    'last_action': self.last_action, 'last_error': self.last_error}

    def event_body(self, service):
        if service == 'ConnectionManager':
            info = self.values(service, 'GetProtocolInfo', {})
            values = {'SourceProtocolInfo': info['Source'], 'SinkProtocolInfo': info['Sink'], 'CurrentConnectionIDs': '0'}
        else:
            if service == 'AVTransport':
                fields = self.values(service, 'GetTransportInfo', {'InstanceID': 0})
                state = fields['CurrentTransportState']
                if state == 'TRANSITIONING': return None
                if state == 'STOPPED' and time.monotonic() - self.last_set_uri < 0.5: return None
                attrs = {'TransportState': state}
                ns = 'urn:schemas-upnp-org:metadata-1-0/AVT/'
            else:
                args = {'InstanceID': 0, 'Channel': 'Master'}
                attrs = {'Volume': self.values(service, 'GetVolume', args)['CurrentVolume'], 'Mute': self.values(service, 'GetMute', args)['CurrentMute']}
                ns = 'urn:schemas-upnp-org:metadata-1-0/RCS/'
            event = ET.Element('Event', {'xmlns': ns})
            inst = ET.SubElement(event, 'InstanceID', {'val': '0'})
            for key, val in attrs.items():
                props = {'val': val}
                if service == 'RenderingControl': props['channel'] = 'Master'
                ET.SubElement(inst, key, props)
            values = {'LastChange': self.restore(ET.tostring(event)).decode()}
        root = ET.Element('e:propertyset', {'xmlns:e': EVENT})
        for k, v in values.items():
            ET.SubElement(ET.SubElement(root, 'e:property'), k).text = v
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)

    def deliver_event(self, sid, sub):
        service = sub['service']
        if service == 'AVTransport':
            with self.transport_changed:
                if self.transport_pending: return
            with self.transport_lock:
                revision = self.transport_revision
                body = self.event_body(service)
        else:
            revision = None
            body = self.event_body(service)
        if body is None or body == sub.get('last'): return
        if revision is not None and revision != self.transport_revision:
            self.event_wake.set(); return
        url = urllib.parse.urlsplit(sub['callback'])
        # Do not hold a control lock during callbacks: clients may synchronously query us.
        conn = http.client.HTTPConnection(sub['peer'], url.port or 80, timeout=2)
        try:
            conn.request('NOTIFY', urllib.parse.urlunsplit(('', '', url.path or '/', url.query, '')), body, {'Content-Type': 'text/xml; charset="utf-8"', 'NT': 'upnp:event', 'NTS': 'upnp:propchange', 'SID': sid, 'SEQ': str(sub['seq'])})
            response = conn.getresponse(); response.read(4096)
            if response.status // 100 != 2: raise RuntimeError('Callback rejected')
        finally: conn.close()
        if service == 'AVTransport':
            event = xml(xml(body).find('.//LastChange').text.encode())
            state = event.find('.//{*}TransportState').attrib['val']
            LOG.info('NOTIFY peer=%s state=%s seq=%s accepted', sub['peer'], state, sub['seq'])
        sub['seq'] = (sub['seq'] + 1) % 4294967296 or 1
        sub['last'] = body
        if revision is not None and revision != self.transport_revision:
            self.event_wake.set()
        with self.lock: self.stats['event_notifications'] += 1

    def events(self):
        while not self.stop.is_set():
            self.event_wake.wait(1); self.event_wake.clear()
            if self.stop.is_set(): break
            with self.lock:
                self.subs = {sid: sub for sid, sub in self.subs.items() if sub['expires'] > time.monotonic()}
                items = list(self.subs.items())
            for sid, sub in items:
                try: self.deliver_event(sid, sub)
                except Exception as e:
                    LOG.warning('Event delivery to %s for %s failed: %s', sub['peer'], sub['service'], type(e).__name__)

    def ssdp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'): s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(('', 1900))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton('239.255.255.250') + socket.inet_aton(self.host))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.host))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(1)
        pending, next_alive = [], 0
        def usn(t): return self.udn if t == self.udn else self.udn + '::' + t
        def announce(kind):
            for t in self.targets:
                msg = 'NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nCACHE-CONTROL: max-age=180\r\nLOCATION: %s/description.xml\r\nNT: %s\r\nNTS: ssdp:%s\r\nSERVER: Darwin/1.0 UPnP/1.0 SonosDLNABridge/1.0\r\nUSN: %s\r\n\r\n' % (self.base, t, kind, usn(t))
                s.sendto(msg.encode(), ('239.255.255.250', 1900))
        LOG.info('SSDP ready on %s:1900', self.host)
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= next_alive:
                    announce('alive'); next_alive = now + 60
                for due, addr, t in pending[:]:
                    if due <= now:
                        msg = 'HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=180\r\nEXT:\r\nLOCATION: %s/description.xml\r\nSERVER: Darwin/1.0 UPnP/1.0 SonosDLNABridge/1.0\r\nST: %s\r\nUSN: %s\r\n\r\n' % (self.base, t, usn(t))
                        s.sendto(msg.encode(), addr); pending.remove((due, addr, t))
                        with self.lock: self.stats['discovery_responses'] += 1
                try: data, addr = s.recvfrom(8192)
                except socket.timeout: continue
                if not data.startswith(b'M-SEARCH ') or not ipaddress.ip_address(addr[0]).is_private: continue
                headers = dict(line.split(':', 1) for line in data.decode(errors='replace').split('\r\n')[1:] if ':' in line)
                headers = {k.strip().lower(): v.strip() for k, v in headers.items()}
                if headers.get('man', '').strip('"') != 'ssdp:discover': continue
                target = headers.get('st')
                try: mx = max(0.1, min(float(headers.get('mx', '1')), 2))
                except ValueError: continue
                if len(pending) > 256: continue
                for t in self.targets:
                    if target in (t, 'ssdp:all'): pending.append((now + random.uniform(0, mx), addr, t))
        finally:
            announce('byebye'); s.close()

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'SonosDLNABridge/1.0'
    def log_message(self, *args): pass
    def log_request(self, code='-', size='-'):
        if isinstance(code, int) and code >= 400:
            kind = self.path.split('/')[1] if '/' in self.path else 'unknown'
            LOG.warning('HTTP request failed: peer=%s method=%s route=%s status=%s', self.client_address[0], self.command, kind, code)
    @property
    def b(self): return self.server.bridge
    def setup(self):
        super().setup(); self.connection.settimeout(15)
    def respond(self, code, body=b'', content_type='text/xml; charset=utf-8', headers=None):
        self.send_response(code); self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        if self.path == '/description.xml':
            with self.b.lock: self.b.stats['description_requests'] += 1
            return self.respond(200, self.b.description)
        if self.path in ('/', '/status'):
            return self.respond(200, json.dumps(self.b.status(), ensure_ascii=False).encode(), 'application/json; charset=utf-8')
        for service in SERVICES:
            if self.path == '/' + service + '.xml':
                LOG.info('%s SCPD %s', self.client_address[0], service)
                return self.respond(200, self.b.scpds[service])
        if self.path.startswith('/media/'): return self.stream()
        self.respond(404)
    def do_POST(self):
        service = self.path.removeprefix('/control/')
        if service not in SERVICES: return self.respond(404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 262144: raise ValueError('Invalid request size')
            root = xml(self.rfile.read(length))
            action_node = root.find('{%s}Body' % SOAP)[0]
            action = action_node.tag.split('}')[-1]
            if action not in SERVICES[service]: return self.fault(401, 'Invalid Action')
            args = {e.tag.split('}')[-1]: e.text or '' for e in action_node}
            safe_args = {k: v for k, v in args.items() if k in ('InstanceID','Speed','Unit','Target','Channel')}
            header_action = self.headers.get('SOAPACTION', '').split('#')[-1].strip('"')
            LOG.info('Request peer=%s body=%s header=%s args=%s', self.client_address[0], action, header_action, safe_args)
            if args.get('InstanceID', '0') != '0': return self.fault(718, 'Invalid InstanceID')
            if action in ('SetAVTransportURI', 'SetNextAVTransportURI'):
                key = 'CurrentURI' if action == 'SetAVTransportURI' else 'NextURI'
                old = args.get(key, '')
                if old:
                    new = self.b.media_url(old, self.client_address[0]); args[key] = new
                    metadata = key + 'MetaData'
                    if args.get(metadata): args[metadata] = args[metadata].replace(old, new).replace(escape(old), escape(new))
            status, data = self.b.client_call(service, action, args)
            with self.b.lock:
                self.b.stats['soap_requests'] += 1
                self.b.last_action = {'action': action, 'time': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
                if status != 200: self.b.last_error = {'action': action, 'http_status': status}
            detail = ''
            if action == 'GetTransportInfo' and status == 200:
                detail = ' state=' + (xml(data).find('.//CurrentTransportState').text or '')
            LOG.info('%s %s -> %d%s', self.client_address[0], action, status, detail)
            self.respond(status, self.b.restore(data))
            if status == 200 and not action.startswith('Get'): self.b.event_wake.set()
        except (ValueError, ET.ParseError, IndexError, AttributeError): self.fault(402, 'Invalid Args')
        except Exception as e:
            LOG.warning('SOAP failed: %s', type(e).__name__); self.fault(501, 'Action Failed')
    def fault(self, code, description):
        body = ('<s:Envelope xmlns:s="%s"><s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>%d</errorCode><errorDescription>%s</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>' % (SOAP, code, description)).encode()
        self.respond(500, body)
    def do_SUBSCRIBE(self):
        service = self.path.removeprefix('/event/')
        LOG.info('%s SUBSCRIBE %s renewal=%s', self.client_address[0], service, bool(self.headers.get('SID')))
        if service not in SERVICES: return self.respond(404)
        sid = self.headers.get('SID')
        with self.b.lock:
            if sid:
                sub = self.b.subs.get(sid)
                if not sub or sub['peer'] != self.client_address[0] or sub['service'] != service: return self.respond(412)
                sub['expires'] = time.monotonic() + 300
            else:
                callback = self.headers.get('CALLBACK', '').strip().strip('<>')
                try:
                    u = urllib.parse.urlsplit(callback)
                    # Never use subscriptions to send requests to unrelated computers.
                    if u.scheme != 'http' or not u.hostname or u.username or u.password: raise ValueError('Invalid callback URL')
                    addresses = {a[4][0] for a in socket.getaddrinfo(u.hostname, u.port or 80, type=socket.SOCK_STREAM)}
                    if addresses != {self.client_address[0]}: raise ValueError('Callback does not match subscriber')
                    if self.headers.get('NT') != 'upnp:event' or len(self.b.subs) >= 64: raise ValueError()
                except (ValueError, OSError) as e:
                    LOG.warning('Subscription rejected from %s: %s', self.client_address[0], type(e).__name__)
                    return self.respond(412)
                sid = 'uuid:' + str(uuid.uuid4())
                self.b.subs[sid] = {'peer': self.client_address[0], 'service': service, 'callback': callback, 'expires': time.monotonic() + 300, 'seq': 0}
        LOG.info('Subscription accepted: %s %s', self.client_address[0], service)
        self.respond(200, headers={'SID': sid, 'TIMEOUT': 'Second-300'})
        self.b.event_wake.set()
    def do_UNSUBSCRIBE(self):
        sid = self.headers.get('SID')
        with self.b.lock:
            sub = self.b.subs.get(sid)
            if not sub or sub['peer'] != self.client_address[0] or self.path != '/event/' + sub['service']: return self.respond(412)
            del self.b.subs[sid]
        self.respond(200)
    def stream(self):
        with self.b.lock: item = self.b.media.get(self.path)
        if not item: return self.respond(404)
        if self.client_address[0] not in (self.b.target, self.b.host, item[1]): return self.respond(403)
        url, owner = item
        conn = None
        started = False
        try:
            for redirect in range(6):
                u = urllib.parse.urlsplit(url)
                if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password: raise ValueError('Invalid media scheme')
                port = u.port or (443 if u.scheme == 'https' else 80)
                ips = media_addresses(u.hostname, port, owner)
                headers = {'User-Agent': 'SonosDLNABridge/1.0', 'Accept': '*/*', 'Accept-Encoding': 'identity'}
                if 'Range' in self.headers: headers['Range'] = self.headers['Range']
                if 'If-Range' in self.headers: headers['If-Range'] = self.headers['If-Range']
                cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
                conn = cls(u.hostname, port, timeout=20)
                # Pin the checked IP while keeping the original hostname for TLS/SNI.
                conn._create_connection = lambda addr, timeout, source_address=None: socket.create_connection((ips[0], port), timeout, source_address)
                conn.request(self.command, urllib.parse.urlunsplit(('', '', u.path or '/', u.query, '')), headers=headers)
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location: raise ValueError('Missing redirect')
                    url = urllib.parse.urljoin(url, location); conn.close(); continue
                if response.status not in (200, 206, 416): raise ValueError('Media HTTP status %d' % response.status)
                self.send_response(response.status)
                for header in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified'):
                    value = response.getheader(header)
                    if value is not None: self.send_header(header, value)
                self.send_header('transferMode.dlna.org', 'Streaming')
                self.send_header('Connection', 'close'); self.close_connection = True
                self.end_headers(); started = True
                with self.b.lock: self.b.stats['media_requests'] += 1
                if self.command != 'HEAD':
                    while True:
                        chunk = response.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
                        with self.b.lock: self.b.stats['media_bytes'] += len(chunk)
                return
            raise ValueError('Too many redirects')
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as e:
            LOG.warning('Media forwarding failed: %s%s', type(e).__name__, (': ' + str(e)) if isinstance(e, ValueError) else '')
            with self.b.lock: self.b.last_error = {'media': type(e).__name__}
            if not started: self.respond(502)
            else: self.close_connection = True
        finally:
            if conn: conn.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, help='Sonos IP address or hostname')
    parser.add_argument('--bind')
    parser.add_argument('--port', type=int, default=17863)
    parser.add_argument('--name', default='Sonos DLNA')
    parser.add_argument('--log')
    parser.add_argument('--state')
    args = parser.parse_args()
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(args.log, maxBytes=2_000_000, backupCount=2) if args.log else logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s')); LOG.addHandler(handler)
    target = socket.gethostbyname(args.target)
    b = Bridge(target, args.bind or local_ip(target), args.port, args.name)
    if args.state:
        b.state_path = Path(args.state)
        if b.state_path.exists():
            saved = json.loads(b.state_path.read_text())
            b.media.update(list(saved.items())[-128:])
    server = ThreadingHTTPServer((b.host, b.port), Handler); server.daemon_threads = True; server.bridge = b
    def end(*_):
        b.stop.set(); threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, end); signal.signal(signal.SIGINT, end)
    def discovery():
        try: b.ssdp()
        except Exception:
            LOG.exception('SSDP failed; restarting service required'); end()
    ssdp_thread = threading.Thread(target=discovery, daemon=True); ssdp_thread.start()
    threading.Thread(target=b.events, daemon=True).start()
    def network_watch():
        while not b.stop.wait(30):
            try:
                current_target = socket.gethostbyname(args.target)
                current_host = args.bind or local_ip(current_target)
                if (current_target, current_host) != (b.target, b.host):
                    LOG.info('Network address changed; restarting for rediscovery')
                    end(); return
            except OSError:
                pass
    threading.Thread(target=network_watch, daemon=True).start()
    LOG.info('Ready: %s at %s -> Sonos %s', b.name, b.base, b.target)
    try: server.serve_forever(poll_interval=0.5)
    finally: b.stop.set(); ssdp_thread.join(timeout=2); server.server_close()

if __name__ == '__main__': main()
