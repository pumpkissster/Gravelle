"""Real HTTP boundaries for password protected encrypted guest reading."""
from contextlib import closing
import base64
import hashlib
import http.client
from http.cookies import SimpleCookie
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image
from server import COOKIE_NAME, Library, ReaderServer

ROOT = Path(__file__).resolve().parents[1]


class SharingAPITests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'work' / 'sharing-api-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        for name, color in [('Shared work', 'crimson'), ('Private work', 'navy')]:
            directory = self.media / name
            directory.mkdir()
            for index in range(2):
                Image.new('RGB', (100, 150), color).save(directory / f'{index+1}.png')
        self.original = (self.media / 'Shared work' / '1.png').read_bytes()
        with zipfile.ZipFile(self.media / 'Archive work.cbz','w') as archive:
            archive.writestr('1.png',self.original)
            archive.writestr('2.png',self.original)
        images = [Image.new('RGB',(100,150),color) for color in ('teal','orange')]
        try:
            images[0].save(self.media / 'PDF work.pdf','PDF',save_all=True,append_images=images[1:],resolution=72)
        finally:
            for image in images:
                image.close()
        self.app = Library(self.media, self.root / 'data', '', 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = self.app.image_search.auto_start = False
        self.app.register({'username':'share-admin', 'password':'generated-admin-password'}, 'fixture')
        self.library = self.app.save_library({'name':'Generated shares', 'path':str(self.media), 'media_type':'photo'})['library']
        end = time.monotonic()+20
        while self.app.scanning and time.monotonic()<end:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)
        books = self.app.listing()['books']
        self.book = next(book for book in books if book['title']=='Shared work')
        self.private = next(book for book in books if book['title']=='Private work')
        child = self.app.save_user({'username':'share-child', 'password':'generated-child-password', 'library_ids':[self.library['id']]})['user']
        self.cookies = {'admin':f'{COOKIE_NAME}={self.app.make_session(1)}', 'child':f"{COOKIE_NAME}={self.app.make_session(child['id'])}"}
        self.server = ReaderServer(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, body=None, actor=None, cookie=None, headers=None, method=None):
        metadata = {'Cookie':self.cookies[actor]} if actor else {}
        if cookie:
            metadata['Cookie'] = cookie
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            metadata['Content-Type'] = 'application/json'
        metadata.update(headers or {})
        with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)) as connection:
            connection.request(method or ('POST' if body is not None else 'GET'), path, content, metadata)
            response = connection.getresponse()
            data = response.read()
            if 'application/json' in response.getheader('Content-Type','') and data:
                data = json.loads(data)
            return response.status, data, dict(response.headers)

    def create(self, **values):
        status, result, _ = self.request('/api/shares', {'book_id':self.book['id'], 'mode':'time', 'days':1, **values}, actor='admin')
        self.assertEqual(status, 201, result)
        return result

    def unlock(self, result, password=None, cookie=None, headers=None):
        token = result['share']['token']
        status, data, metadata = self.request('/api/share/'+token+'/unlock', {'password':password if password is not None else result['password']}, cookie=cookie, headers=headers)
        parsed = SimpleCookie()
        parsed.load(metadata.get('Set-Cookie',''))
        return status, data, metadata, ('gravelle_share='+parsed['gravelle_share'].value if 'gravelle_share' in parsed else '')

    def test_admin_only_creation_listing_and_close(self):
        body = {'book_id':self.book['id'],'mode':'time','days':1}
        self.assertEqual(self.request('/api/shares',body)[0],401)
        self.assertEqual(self.request('/api/shares',body,'child')[0],403)
        self.assertEqual(self.request('/api/shares',actor='child')[0],403)
        result = self.create()
        self.assertGreaterEqual(len(result['password']),8)
        self.assertNotIn('password_hash',result['share'])
        shares = self.request('/api/shares',actor='admin')[1]['shares']
        self.assertEqual(len(shares),1)
        self.assertNotIn('password',shares[0])
        self.assertEqual(self.request('/api/shares/'+shares[0]['id']+'/close',{},'child')[0],403)
        self.assertEqual(self.request('/api/shares/'+shares[0]['id']+'/close',{},'admin')[0],200)
        self.assertEqual(self.request('/api/shares',actor='admin')[1]['shares'][0]['status'],'closed')

    def test_public_shell_is_inert_and_initial_info_discloses_no_work(self):
        result = self.create()
        token = result['share']['token']
        status, html, headers = self.request('/share/'+token)
        self.assertEqual(status,200)
        self.assertIn(b'/share-reader.js',html)
        self.assertNotIn(b'/app.js',html)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn('noindex',headers['X-Robots-Tag'])
        self.assertEqual(headers['Referrer-Policy'],'no-referrer')
        info = self.request('/api/share/'+token)[1]
        self.assertEqual(info,{'status':'active','requires_password':True})
        for fragment in ('title','path','source_root','book_id','key','password'):
            self.assertNotIn(fragment,info)
        self.assertEqual(self.request('/api/share/'+token+'/manifest')[0],401)
        self.assertEqual(self.request('/api/share/'+token+'/pages/0')[0],401)

    def test_password_reveal_is_owner_admin_only_and_never_part_of_listing(self):
        result = self.create(password='generated-reveal-password')
        route = '/api/shares/'+result['share']['id']+'/password'
        self.assertTrue(result['share']['password_available'])
        self.assertEqual(result['share']['visits'],0)
        self.assertEqual(self.request(route,{})[0],401)
        self.assertEqual(self.request(route,{},actor='child')[0],403)
        _,_,_,cookie=self.unlock(result)
        self.assertEqual(self.request(route,{},cookie=cookie)[0],401)
        status,body,headers=self.request(route,{},actor='admin')
        self.assertEqual(status,200,body)
        self.assertEqual(body,{'password':result['password']})
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.request(route+'?password=1',{},actor='admin')[0],400)
        self.assertEqual(self.request(route,{'reveal':True},actor='admin')[0],400)
        self.assertEqual(self.request(route,actor='admin')[0],404)
        listing=self.request('/api/shares',actor='admin')[1]
        encoded=json.dumps(listing)
        for value in (result['password'],'password_hash','password_ciphertext','password_nonce','encryption_key'):
            self.assertNotIn(value,encoded)
        # Administrative copies remain available for closed records, without
        # reopening guest access or changing the already accumulated count.
        self.assertEqual(self.request('/api/shares/'+result['share']['id']+'/close',{},actor='admin')[0],200)
        self.assertEqual(self.request(route,{},actor='admin')[1],body)

    def test_visits_count_only_successful_new_sessions_and_survive_cookie_reuse(self):
        result=self.create(days=3)
        token=result['share']['token']
        def visits():
            return self.request('/api/shares',actor='admin')[1]['shares'][0]['visits']
        self.assertEqual(visits(),0)
        self.assertEqual(self.unlock(result,password='incorrect')[0],403)
        self.assertEqual(visits(),0)
        _,_,_,cookie=self.unlock(result)
        self.assertEqual(visits(),1)
        self.assertEqual(self.unlock(result,cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/share/'+token+'/manifest',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/share/'+token+'/pages/0',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/share/'+token+'/pages/1',cookie=cookie)[0],200)
        self.assertEqual(visits(),1)
        self.assertEqual(self.unlock(result)[0],200)
        self.assertEqual(visits(),2)
        # This is a statistic, not a configurable visit limit.
        self.assertEqual(self.request('/api/share/'+token+'/manifest',cookie=cookie)[0],200)

    def test_hash_only_old_share_keeps_working_but_has_no_recoverable_password(self):
        result=self.create()
        with closing(self.app.connect()) as connection,connection:
            connection.execute('UPDATE shares SET password_encrypted=NULL WHERE id=?',(result['share']['id'],))
        listing=self.request('/api/shares',actor='admin')[1]['shares'][0]
        self.assertFalse(listing['password_available'])
        status,body,_=self.request('/api/shares/'+result['share']['id']+'/password',{},actor='admin')
        self.assertEqual(status,409,body)
        self.assertNotIn('password',body)
        self.assertEqual(self.unlock(result)[0],200)

    def test_ciphertext_and_cookie_never_authorize_normal_account_apis(self):
        result = self.create(password='share-password-123')
        token = result['share']['token']
        self.assertEqual(self.unlock(result,password='bad')[0],403)
        status, visitor, metadata, cookie = self.unlock(result)
        self.assertEqual(status,200,visitor)
        self.assertNotIn('session',visitor)
        self.assertEqual(set(visitor['manifest']),{'title','page_count','format'})
        self.assertIn('HttpOnly',metadata['Set-Cookie'])
        self.assertIn('SameSite=Strict',metadata['Set-Cookie'])
        self.assertIn('Path=/api/share/'+token,metadata['Set-Cookie'])
        path = '/api/share/'+token+'/pages/0'
        status, packet, headers = self.request(path,cookie=cookie)
        self.assertEqual(status,200,packet)
        self.assertEqual(headers['Content-Type'],'application/octet-stream')
        self.assertIn('no-store',headers['Cache-Control'])
        self.assertNotIn('ETag',headers)
        self.assertTrue(packet.startswith(b'GSH1'))
        self.assertNotIn(self.original,packet)
        key = base64.urlsafe_b64decode(visitor['key']+'=')
        plain = AESGCM(key).decrypt(packet[4:16],packet[17:],packet[:17]+('gravelle-share-v1:'+token+':0').encode())
        self.assertEqual(plain,self.original)
        self.assertNotEqual(self.request(path,cookie=cookie)[1],packet)
        self.assertEqual(self.request('/api/books/'+self.book['id']+'/pages/0',cookie=cookie)[0],401)
        self.assertEqual(self.request('/api/library',cookie=cookie)[0],401)
        self.assertEqual(self.request('/api/shares',cookie=cookie)[0],401)
        self.assertEqual(self.request(path)[0],401)
        self.assertEqual(self.request(path+'?original=1',cookie=cookie)[0],400)
        self.assertEqual(self.request(path,cookie=cookie,method='HEAD')[1],b'')
        self.assertEqual(self.request('/api/share/'+token+'/pages/2',cookie=cookie)[0],404)
        self.assertEqual((self.media/'Shared work'/'1.png').read_bytes(),self.original)

    def test_three_day_share_allows_independent_visitors_and_rejects_count_creation(self):
        result=self.create(days=3)
        self.assertAlmostEqual(result['share']['expires_at']-result['share']['created_at'],3*86400)
        self.assertNotIn('max_visits',result['share'])
        _,_,_,cookie=self.unlock(result)
        token=result['share']['token']
        self.assertEqual(self.request('/api/share/'+token+'/manifest',cookie=cookie)[0],200)
        self.assertEqual(self.unlock(result,cookie=cookie)[0],200)
        self.assertEqual(self.unlock(result)[0],200)
        self.assertEqual(self.request('/api/shares',{'book_id':self.book['id'],'mode':'visits','max_visits':1},actor='admin')[0],400)

    def test_real_cbz_and_pdf_sources_remain_readable_through_encryption(self):
        books = self.app.listing()['books']
        for expected in ('cbz','pdf'):
            book = next(item for item in books if item['format']==expected)
            result = self.create(book_id=book['id'])
            status, visitor, _, cookie = self.unlock(result)
            self.assertEqual(status,200,visitor)
            self.assertEqual(visitor['manifest']['format'],expected)
            token = result['share']['token']
            status, packet, _ = self.request('/api/share/'+token+'/pages/1?width=800',cookie=cookie)
            self.assertEqual(status,200,packet)
            key = base64.urlsafe_b64decode(visitor['key']+'=')
            data = AESGCM(key).decrypt(packet[4:16],packet[17:],packet[:17]+('gravelle-share-v1:'+token+':1').encode())
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.format,'JPEG' if expected=='pdf' else 'PNG')
                if expected=='pdf':
                    self.assertEqual(image.width,800)

    def test_guest_exit_clears_cookie_and_revokes_old_session_but_not_other_visitors(self):
        result = self.create()
        base = '/api/share/'+result['share']['token']
        _,_,_,first_cookie = self.unlock(result)
        _,_,_,second_cookie = self.unlock(result)
        self.assertEqual(self.request(base+'/logout?all=1',{},cookie=first_cookie)[0],400)
        self.assertEqual(self.request(base+'/logout',{'all':True},cookie=first_cookie)[0],400)
        self.assertEqual(self.request(base+'/logout',{},cookie=first_cookie,headers={'Origin':'https://other.invalid'})[0],403)
        status,body,headers = self.request(base+'/logout',{},cookie=first_cookie)
        self.assertEqual(status,200,body)
        self.assertEqual(body,{'logged_out':True})
        self.assertEqual(headers['Cache-Control'],'no-store')
        for fragment in ('Max-Age=0','Path='+base,'HttpOnly','SameSite=Strict','expires=Thu, 01 Jan 1970'):
            self.assertIn(fragment,headers['Set-Cookie'])
        # A copied pre-exit cookie is invalid server-side, not just hidden
        # by the document; a page reload cannot restore its reading session.
        self.assertEqual(self.request(base+'/manifest',cookie=first_cookie)[0],401)
        self.assertEqual(self.request(base+'/pages/0',cookie=first_cookie)[0],401)
        self.assertTrue(self.request(base,cookie=first_cookie)[1]['requires_password'])
        self.assertEqual(self.request(base+'/manifest',cookie=second_cookie)[0],200)
        self.assertEqual(self.request(base+'/pages/0',cookie=second_cookie)[0],200)
        self.assertEqual(self.request('/api/shares',actor='admin')[1]['shares'][0]['visits'],2)
        self.assertEqual(self.unlock(result,password='incorrect',cookie=first_cookie)[0],403)
        self.assertEqual(self.request(base+'/logout',{},cookie=first_cookie)[0],200)
        self.assertEqual(self.request(base+'/logout',{})[0],200)
        status,_,_,fresh_cookie = self.unlock(result,cookie=first_cookie)
        self.assertEqual(status,200)
        self.assertNotEqual(fresh_cookie,first_cookie)
        self.assertEqual(self.request('/api/shares',actor='admin')[1]['shares'][0]['visits'],3)

    def test_exit_remains_available_after_link_is_closed_and_uses_secure_cookie_scope(self):
        result = self.create()
        base = '/api/share/'+result['share']['token']
        _,_,_,cookie = self.unlock(result)
        self.assertEqual(self.request('/api/shares/'+result['share']['id']+'/close',{},actor='admin')[0],200)
        status,_,headers = self.request(base+'/logout',{},cookie=cookie,headers={'X-Forwarded-Proto':'https'})
        self.assertEqual(status,200)
        self.assertIn('Secure',headers['Set-Cookie'])
        self.assertIn('Max-Age=0',headers['Set-Cookie'])

    def test_closed_record_deletion_and_resharing_leave_new_link_and_source_intact(self):
        old=self.create(days=3)
        old_base='/api/share/'+old['share']['token']
        _,_,_,old_cookie=self.unlock(old)
        action='/api/shares/'+old['share']['id']
        self.assertEqual(self.request(action+'/delete',{})[0],401)
        self.assertEqual(self.request(action+'/delete',{},actor='child')[0],403)
        self.assertEqual(self.request(action+'/delete',{},actor='admin')[0],409)
        self.assertEqual(self.request(action+'/close',{},actor='admin')[0],200)
        new=self.create(days=7)
        self.assertNotEqual(new['share']['token'],old['share']['token'])
        self.assertNotEqual(new['password'],old['password'])
        self.assertEqual(self.request(old_base+'/pages/0',cookie=old_cookie)[0],410)
        self.assertEqual(len(self.request('/api/shares',actor='admin')[1]['shares']),2)
        self.assertEqual(self.request(action+'/delete?all=1',{},actor='admin')[0],400)
        self.assertEqual(self.request(action+'/delete',{'book_id':self.book['id']},actor='admin')[0],400)
        self.assertEqual(self.request(action+'/delete',{},actor='admin')[0],200)
        self.assertEqual(self.request(old_base+'/manifest',cookie=old_cookie)[0],404)
        self.assertEqual(len(self.request('/api/shares',actor='admin')[1]['shares']),1)
        _,_,_,new_cookie=self.unlock(new)
        self.assertEqual(self.request('/api/share/'+new['share']['token']+'/pages/0',cookie=new_cookie)[0],200)
        self.assertEqual((self.media/'Shared work'/'1.png').read_bytes(),self.original)

    def test_revoke_and_expiry_reject_old_image_urls_even_with_cache_headers(self):
        result = self.create()
        _, _, _, cookie = self.unlock(result)
        token = result['share']['token']
        path = '/api/share/'+token+'/pages/0'
        self.assertEqual(self.request(path,cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/shares/'+result['share']['id']+'/close',{},actor='admin')[0],200)
        self.assertEqual(self.request(path,cookie=cookie,headers={'If-None-Match':'*'})[0],410)
        self.assertEqual(self.request('/api/share/'+token+'/manifest',cookie=cookie)[0],410)
        result = self.create()
        _, _, _, cookie = self.unlock(result)
        with closing(self.app.connect()) as c,c:
            c.execute('UPDATE shares SET expires_at=? WHERE id=?',(time.time()-1,result['share']['id']))
        self.assertEqual(self.request('/api/share/'+result['share']['token']+'/pages/0',cookie=cookie)[0],410)

    def test_origin_checks_body_queries_and_secure_cookie(self):
        result = self.create()
        token = result['share']['token']
        self.assertEqual(self.unlock(result,headers={'Origin':'https://external.invalid'})[0],403)
        self.assertEqual(self.request('/api/share/'+token+'/unlock?password=bad',{'password':result['password']})[0],400)
        self.assertEqual(self.request('/api/share/'+token+'/unlock',{'password':result['password'],'book_id':self.private['id']})[0],400)
        self.assertEqual(self.request('/api/shares?mode=visits',{'book_id':self.book['id']},actor='admin')[0],400)
        status, _, headers, cookie = self.unlock(result,headers={'X-Forwarded-Proto':'https'})
        self.assertEqual(status,200)
        self.assertIn('Secure',headers['Set-Cookie'])
        self.assertEqual(self.request('/api/share/'+token+'/pages/0?width=900',cookie=cookie)[0],400)
        self.assertEqual(self.request('/api/share/'+token+'/pages/0?width=800&width=1600',cookie=cookie)[0],400)
        with closing(self.app.connect()) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_expiry_or_closure_during_image_read_does_not_release_ciphertext(self):
        result = self.create()
        _, _, _, cookie = self.unlock(result)
        original = self.app.image
        def expire(*args,**kwargs):
            data = original(*args,**kwargs)
            with closing(self.app.connect()) as c,c:
                c.execute('UPDATE shares SET expires_at=? WHERE id=?',(time.time()-1,result['share']['id']))
            return data
        with patch.object(self.app,'image',side_effect=expire):
            status, body, _ = self.request('/api/share/'+result['share']['token']+'/pages/0',cookie=cookie)
        self.assertEqual(status,410,body)
        self.assertIsInstance(body,dict)
        self.assertNotIn('key',body)

    def test_public_unlock_rejects_ambiguous_framing_and_redacts_invalid_path_tokens(self):
        result = self.create()
        token = result['share']['token']
        payload = json.dumps({'password':result['password']}).encode()
        for headers in ([('Content-Length',str(len(payload))),('Content-Length',str(len(payload)))],
                        [('Content-Length',str(len(payload))),('Content-Encoding','gzip')],
                        [('Content-Length','+20')]):
            with closing(http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)) as connection:
                connection.putrequest('POST','/api/share/'+token+'/unlock')
                connection.putheader('Content-Type','application/json')
                for name,value in headers:
                    connection.putheader(name,value)
                connection.endheaders(payload)
                response = connection.getresponse()
                self.assertEqual(response.status,400,response.read())
                response.read()
                self.assertEqual(connection.sock.recv(1),b'')
        with self.assertLogs('sumi',level='INFO') as captured:
            self.assertEqual(self.request('/api/share/'+token+'%2F/manifest')[0],404)
        self.assertNotIn(token,'\n'.join(captured.output))
        self.assertIn('[redacted]','\n'.join(captured.output))


if __name__=='__main__':
    unittest.main()
