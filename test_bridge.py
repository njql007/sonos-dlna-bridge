"""Protocol/stream tests using a fake speaker; never changes the real Sonos."""
import collections
import socket
import struct
import http.client
import threading
import time
import unittest
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from bridge import Bridge, Handler, SOAP, soap, xml, media_addresses, media_duration, discover_sonos, select_sonos, speaker_description
from unittest.mock import patch, MagicMock

class Origin(BaseHTTPRequestHandler):
    seen = []
    audio = b'RIFF' + bytes(range(256)) * 1000
    def log_message(self, *args): pass
    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        action = xml(raw).find('{%s}Body' % SOAP)[0]
        name = action.tag.split('}')[-1]
        args = {e.tag.split('}')[-1]: e.text or '' for e in action}
        self.seen.append((name, args))
        if name == 'Seek':
            body = b'<error>Unsupported seek</error>'; status = 500
        else:
            body = soap('AVTransport', name+'Response', {'TrackURI': args.get('CurrentURI','')}); status = 200
        self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302);self.send_header('Location','/audio.wav');self.end_headers();return
        if self.path == '/bad-redirect':
            self.send_response(302);self.send_header('Location','http://192.0.2.123/private');self.end_headers();return
        content = self.audio; status = 200
        if self.headers.get('Range') == 'bytes=4-15': content = content[4:16];status=206
        self.send_response(status);self.send_header('Content-Length',str(len(content)));self.send_header('Content-Type','audio/wav')
        if status == 206:self.send_header('Content-Range','bytes 4-15/%d'%len(self.audio))
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(content)

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
        threading.Thread(target=cls.origin.serve_forever,daemon=True).start()
        cls.origin_url='http://127.0.0.1:%d'%cls.origin.server_port
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        b=Bridge.__new__(Bridge);b.target=b.host='127.0.0.1';b.port=cls.server.server_port
        b.transport_lock=threading.RLock();b.transport_changed=threading.Condition();b.transport_pending=0;b.last_set_uri=0;b.transport_revision=0;b.event_wake=threading.Event();
        b.base='http://127.0.0.1:%d'%b.port;b.name='Test';b.lock=threading.RLock();b.media=collections.OrderedDict();b.subs={};b.stats=collections.Counter();b.last_action=b.last_error=None
        b.controls={k:cls.origin_url+'/'+k for k in ('AVTransport','RenderingControl','ConnectionManager')}
        b.scpds={};b.state_path=None;b.durations={};b.duration_tasks={};cls.b=b;cls.server.bridge=b
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for s in (cls.server,cls.origin):s.shutdown();s.server_close()
    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method,path,body=data,headers=headers or {})
        r=conn.getresponse();answer=(r.status,dict(r.headers),r.read());conn.close();return answer
    def test_action_and_metadata_rewrite(self):
        url=self.origin_url+'/audio.wav?token=private&x=1'
        data=soap('AVTransport','SetAVTransportURI',{'InstanceID':0,'CurrentURI':url,'CurrentURIMetaData':'<DIDL-Lite><item><res>'+url.replace('&','&amp;')+'</res></item></DIDL-Lite>'})
        status,_,body=self.request('POST','/control/AVTransport',data)
        self.assertEqual(status,200);name,args=Origin.seen[-1]
        self.assertEqual(name,'SetAVTransportURI');self.assertTrue(args['CurrentURI'].startswith(self.b.base+'/media/'))
        self.assertIn(args['CurrentURI'],args['CurrentURIMetaData']);self.assertNotIn('private',args['CurrentURI'])
        self.assertEqual(xml(body).find('.//TrackURI').text,url)
    def test_backend_failure_preserved(self):
        status,_,body=self.request('POST','/control/AVTransport',soap('AVTransport','Seek',{'InstanceID':0,'Unit':'REL_TIME','Target':'00:00:03'}))
        self.assertEqual(status,500);self.assertIn(b'Unsupported seek',body)
    def test_media_head_range_and_redirect(self):
        url=self.b.media_url(self.origin_url+'/redirect','127.0.0.1');path=url.replace(self.b.base,'')
        status,headers,body=self.request('HEAD',path);self.assertEqual(status,200);self.assertEqual(int(headers['Content-Length']),len(Origin.audio));self.assertEqual(body,b'')
        status,headers,body=self.request('GET',path,headers={'Range':'bytes=4-15'})
        self.assertEqual(status,206);self.assertEqual(body,Origin.audio[4:16]);self.assertEqual(headers['Content-Range'],'bytes 4-15/%d'%len(Origin.audio))
        status,_,body=self.request('GET',path);self.assertEqual(body,Origin.audio)
    def test_callback_restriction_and_renewal(self):
        self.assertEqual(self.request('SUBSCRIBE','/event/AVTransport',headers={'CALLBACK':'<http://192.0.2.1:1234/>','NT':'upnp:event'})[0],412)
        status,headers,_=self.request('SUBSCRIBE','/event/AVTransport',headers={'CALLBACK':'<http://127.0.0.1:1234/>','NT':'upnp:event'})
        self.assertEqual(status,200);sid=headers['SID']
        self.assertEqual(self.request('SUBSCRIBE','/event/AVTransport',headers={'SID':sid})[0],200)
        self.assertEqual(self.request('UNSUBSCRIBE','/event/AVTransport',headers={'SID':sid})[0],200)
    def test_callback_default_port(self):
        status,headers,_=self.request('SUBSCRIBE','/event/AVTransport',headers={'CALLBACK':'<http://127.0.0.1/event>','NT':'upnp:event'})
        self.assertEqual(status,200)
        self.assertEqual(self.request('UNSUBSCRIBE','/event/AVTransport',headers={'SID':headers['SID']})[0],200)
    def test_mp4_duration_and_position_response(self):
        payload = bytes(12) + struct.pack('>II', 1000, 235034) + bytes(80)
        mvhd = struct.pack('>I4s', len(payload)+8, b'mvhd') + payload
        moov = struct.pack('>I4s', len(mvhd)+8, b'moov') + mvhd
        self.assertAlmostEqual(media_duration(moov),235.034)
        self.assertIsNone(media_duration(moov[:20]))
        path='/media/duration-test.m4a'
        self.b.media[path]=(self.origin_url+'/audio.wav','127.0.0.1');self.b.durations[path]=235.034
        raw=soap('AVTransport','GetPositionInfoResponse',{'TrackURI':self.b.base+path,'TrackDuration':'0:00:00','RelTime':'0:00:12'})
        result=xml(self.b.normalize_response('AVTransport','GetPositionInfo',raw))
        self.assertEqual(result.find('.//TrackDuration').text,'00:03:55')
        self.assertEqual(result.find('.//RelTime').text,'00:00:12')
    def test_transport_event_is_small_and_stable(self):
        with patch.object(self.b,'values',return_value={'CurrentTransportState':'PAUSED_PLAYBACK'}):
            outer=xml(self.b.event_body('AVTransport'))
            inner=xml(outer.find('.//LastChange').text.encode())
            instance=inner.find('{*}InstanceID')
            self.assertEqual(len(instance),1)
            self.assertEqual(instance[0].tag.split('}')[-1],'TransportState')
            self.assertEqual(instance[0].attrib['val'],'PAUSED_PLAYBACK')
        with patch.object(self.b,'values',return_value={'CurrentTransportState':'TRANSITIONING'}):
            self.assertIsNone(self.b.event_body('AVTransport'))
    def test_old_snapshot_not_delivered(self):
        self.b.transport_revision=10
        def snapshot(service):
            self.b.transport_revision+=1
            return b'<old-state/>'
        sub={'service':'AVTransport','callback':'http://127.0.0.1:12345/','peer':'127.0.0.1','seq':0}
        with patch.object(self.b,'event_body',side_effect=snapshot),patch('http.client.HTTPConnection') as conn:
            self.b.deliver_event('uuid:test',sub)
            conn.assert_not_called()
        self.assertTrue(self.b.event_wake.is_set())
    def test_state_read_waits_for_inflight_play(self):
        entered=threading.Event();release=threading.Event();results=[]
        def fake(service,action,args):
            if action=='Play':
                entered.set();release.wait(2)
            else:results.append('read')
            return 200,b'<ok/>'
        with patch.object(self.b,'_client_call',side_effect=fake):
            play=threading.Thread(target=lambda:self.b.client_call('AVTransport','Play',{}));play.start()
            self.assertTrue(entered.wait(1))
            read=threading.Thread(target=lambda:self.b.client_call('AVTransport','GetTransportInfo',{}));read.start()
            time.sleep(.05);self.assertEqual(results,[])
            release.set();play.join(2);read.join(2);self.assertEqual(results,['read'])
    def test_state_read_allows_immediately_following_play(self):
        self.b.last_set_uri=time.monotonic();order=[]
        def fake(service,action,args):
            order.append(action);return 200,b'<ok/>'
        with patch.object(self.b,'_client_call',side_effect=fake):
            read=threading.Thread(target=lambda:self.b.client_call('AVTransport','GetTransportInfo',{}));read.start()
            time.sleep(.03)
            self.b.client_call('AVTransport','Play',{});read.join(2)
        self.b.last_set_uri=0
        self.assertEqual(order,['Play','GetTransportInfo'])
    def test_waits_for_actual_transport_state(self):
        transient=soap('AVTransport','GetTransportInfoResponse',{'CurrentTransportState':'TRANSITIONING'})
        playing=soap('AVTransport','GetTransportInfoResponse',{'CurrentTransportState':'PLAYING'})
        with patch.object(self.b,'call',side_effect=[(200,transient),(200,playing)]):
            status,data=self.b.client_call('AVTransport','GetTransportInfo',{'InstanceID':0})
        self.assertEqual(status,200);self.assertEqual(xml(data).find('.//CurrentTransportState').text,'PLAYING')
    def test_service_description_default_namespace(self):
        element=ET.Element('{urn:schemas-upnp-org:service-1-0}scpd')
        raw=ET.tostring(element)
        self.assertTrue(raw.startswith(b'<scpd xmlns='));self.assertNotIn(b'ns0:',raw)
    def test_invalid_action_and_xml(self):
        for data in (soap('AVTransport','RemoveAllTracksFromQueue',{'InstanceID':0}),b'<!DOCTYPE foo><x/>',b'invalid'):
            self.assertEqual(self.request('POST','/control/AVTransport',data)[0],500)
    def test_netease_fake_ip_scope(self):
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('198.18.0.22', 80))]):
            self.assertEqual(media_addresses('m701.music.126.net',80,'192.168.0.101'),['198.18.0.22'])
            for host in ('example.com','music.126.net.evil.example','evil-music.126.net'):
                with self.assertRaises(ValueError):media_addresses(host,80,'192.168.0.101')
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.0.1', 80))]):
            with self.assertRaises(ValueError):media_addresses('m701.music.126.net',80,'192.168.0.101')
    def test_media_restrictions(self):
        for value in ('file:///etc/passwd','http://name:secret@example.com/x'):
            with self.assertRaises(ValueError):self.b.media_url(value,'127.0.0.1')
        for url in ('http://192.0.2.123/x',self.origin_url+'/bad-redirect'):
            path=self.b.media_url(url,'127.0.0.1').replace(self.b.base,'')
            self.assertEqual(self.request('GET',path)[0],502)

class DiscoveryTests(unittest.TestCase):
    def test_discovery_filters_and_deduplicates(self):
        sock = MagicMock()
        sock.recvfrom.side_effect = [
            (b'HTTP/1.1 200 OK\r\nLOCATION: http://untrusted.example/\r\n', ('192.168.1.10', 1900)),
            (b'HTTP/1.1 200 OK', ('192.168.1.10', 1900)),
            (b'HTTP/1.1 200 OK', ('192.168.1.11', 1900)),
            (b'HTTP/1.1 200 OK', ('8.8.8.8', 1900)),
            (b'NOTIFY * HTTP/1.1', ('192.168.1.12', 1900)),
            socket.timeout(),
        ]
        speaker = {'id': 'uuid:one', 'ip': '192.168.1.10', 'name': 'Room', 'model': 'Test'}
        with patch('bridge.socket.socket') as factory, patch('bridge.speaker_description', side_effect=[speaker, None]) as probe:
            factory.return_value.__enter__.return_value = sock
            self.assertEqual(discover_sonos(bind='192.168.1.2'), [speaker])
        self.assertEqual([call.args[0] for call in probe.call_args_list], ['192.168.1.10', '192.168.1.11'])
        self.assertIn(b'ZonePlayer:1', sock.sendto.call_args.args[0])

    def test_selection_stable_id_and_multiple_rooms(self):
        first = {'id': 'uuid:one', 'ip': '192.168.1.10', 'name': 'A', 'model': 'Test'}
        moved = dict(first, ip='192.168.1.20')
        other = dict(first, id='uuid:two', name='B')
        self.assertEqual(select_sonos([first]), first)
        self.assertEqual(select_sonos([moved, other], 'uuid:one'), moved)
        with patch('builtins.print'), patch('builtins.input', return_value='2'):
            self.assertEqual(select_sonos([first, other], interactive=True), other)
        with patch('builtins.print'):
            with self.assertRaisesRegex(ValueError, 'Multiple'): select_sonos([first, other])
        with self.assertRaisesRegex(ValueError, 'No Sonos'): select_sonos([])
        with self.assertRaisesRegex(ValueError, 'not found'): select_sonos([other], 'uuid:one')

    def test_description_requires_native_sonos(self):
        document = b'<root><device><deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType><manufacturer>Sonos, Inc.</manufacturer><UDN>uuid:one</UDN><roomName>Bedroom</roomName><modelName>Play:3</modelName></device></root>'
        with patch('bridge.OPENER.open') as opener:
            response = opener.return_value.__enter__.return_value
            response.read.return_value = document
            self.assertEqual(speaker_description('192.168.1.10')['name'], 'Bedroom')
            self.assertEqual(opener.call_args.args[0], 'http://192.168.1.10:1400/xml/device_description.xml')
            response.read.return_value = document.replace(b'ZonePlayer:1', b'MediaRenderer:1')
            self.assertIsNone(speaker_description('192.168.1.10'))

if __name__=='__main__':unittest.main(verbosity=2)
