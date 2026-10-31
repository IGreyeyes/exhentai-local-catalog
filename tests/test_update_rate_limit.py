"""Simulate rate-limited APIs and official public release pages without live data."""
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import hashlib
import io
import json
import tempfile
import unittest

import desktop_updates as updates
from client_version import APP_VERSION, PACKAGE_NAME, REPOSITORY
from github_release_page import parse_latest_page, parse_package_asset

NEXT_VERSION='.'.join([*APP_VERSION.split('.')[:2],str(int(APP_VERSION.split('.')[2])+1)])
BASE=f'https://github.com/{REPOSITORY}'
TAG='v'+NEXT_VERSION


class Response(io.BytesIO):
    def __init__(self,body=b'',url='',headers=None,status=200):
        super().__init__(body)
        self.url=url
        self.headers=headers or {}
        self.status=status

    def geturl(self):return self.url


def page(tag=TAG,label='Latest',body=None,fragment=None):
    body=body if body is not None else f'<h2>v{tag[1:]} · 本次更新</h2><ul><li><strong>测试改进</strong>：保留校验。</li></ul><script>do_not_render()</script>'
    fragment=fragment or BASE+'/releases/expanded_assets/'+tag
    return f'<span class="Label Label--success">{label}</span><relative-time datetime="2026-10-01T00:00:00Z"></relative-time><div data-test-selector="body-content">{body}</div><include-fragment src="/irrelevant-navigation"></include-fragment><include-fragment src="{fragment}"></include-fragment>'.encode()


def asset_row(tag=TAG,name=PACKAGE_NAME,digest=None):
    digest=digest or 'sha256:'+hashlib.sha256(b'new').hexdigest()
    return f'<li><a href="/{REPOSITORY}/releases/download/{tag}/{name}">{name}</a><clipboard-copy value="{digest}"></clipboard-copy></li>'.encode()


class RateLimitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.updater=updates.ClientUpdater(self.root)
        self.requests=[]
        self.first=True
        self.code=403
        self.tag=TAG
        self.label='Latest'
        self.digest='sha256:'+hashlib.sha256(b'new').hexdigest()
        self.page_override=None
        self.asset_override=None
        self.head_headers={'Content-Length':'3','Content-Type':'application/octet-stream'}
        self.head_status=200
        self.api_enabled=False
        self.public_error=None
        self.final_url=None
        self.opener=patch.object(self.updater.opener,'open',side_effect=self.respond)
        self.opener.start()
        self.direct=patch.object(self.updater.direct_opener,'open',side_effect=self.respond)
        self.direct.start()
        self.sleeps=patch('desktop_updates.time.sleep')
        self.sleeps.start()

    def tearDown(self):
        self.opener.stop();self.direct.stop();self.sleeps.stop()
        self.directory.cleanup()

    def respond(self,request,**options):
        url=request.full_url
        self.requests.append((request.get_method(),url))
        if url.startswith('https://api.github.com/'):
            if self.api_enabled:
                return Response(json.dumps({'tag_name':APP_VERSION}).encode(),url)
            raise HTTPError(url,self.code,'rate limit exceeded',{'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'1300'},None)
        if self.public_error is not None:raise self.public_error
        if url==BASE+'/releases/latest':
            return Response(self.page_override if self.page_override is not None else page(self.tag,self.label),self.final_url or BASE+'/releases/tag/'+self.tag)
        if '/expanded_assets/' in url:
            return Response(self.asset_override if self.asset_override is not None else asset_row(self.tag,digest=self.digest),url)
        if request.get_method()=='HEAD':return Response(url=url,headers=self.head_headers,status=self.head_status)
        raise AssertionError('Unexpected request '+url)

    def check(self):
        with patch('desktop_updates.time.time',return_value=1000):return self.updater.check()

    def test_rate_limit_uses_public_page_and_platform_digest_with_exact_package_size(self):
        release=self.check()
        self.assertTrue(release['available'])
        self.assertEqual((release['latest_version'],release['size'],release['sha256']),(NEXT_VERSION,3,self.digest[7:]))
        self.assertEqual(release['url'],BASE+'/releases/download/'+TAG+'/'+PACKAGE_NAME)
        self.assertEqual(release['included_versions'],[NEXT_VERSION])
        self.assertIn('**测试改进**',release['notes'])
        self.assertNotIn('do_not_render',release['notes'])
        self.assertIn('公开发布页',release['check_message'])
        self.assertEqual(len(self.requests),4)
        self.assertEqual(self.requests[-1][0],'HEAD')
        self.assertFalse(self.updater.lock.locked())

    def test_429_and_remaining_limit_window_skip_api_until_reset(self):
        self.code=429
        self.check()
        self.check()
        self.assertEqual(sum('api.github.com' in url for _,url in self.requests),1)
        self.api_enabled=True
        with patch('desktop_updates.time.time',return_value=1400):release=self.updater.check()
        self.assertFalse(release['available'])
        self.assertEqual(sum('api.github.com' in url for _,url in self.requests),2)

    def test_current_version_requires_only_release_page_and_uses_current_announcement(self):
        self.tag='v'+APP_VERSION
        release=self.check()
        self.assertFalse(release['available'])
        self.assertEqual(release['included_versions'],[APP_VERSION])
        self.assertEqual(len(self.requests),2)
        self.assertNotIn('url',release)

    def test_foreign_or_invalid_redirect_never_leaves_a_stale_installable_release(self):
        self.updater.release={'available':True}
        for url in ('https://github.com/other/repo/releases/tag/'+TAG,'http://github.com/'+REPOSITORY+'/releases/tag/'+TAG,BASE+'/releases/tag/v1.0.0-beta',BASE+'/releases/tag/'+TAG+'?other=1'):
            self.final_url=url
            with self.subTest(url=url),self.assertRaises(ValueError):self.check()
            self.assertIsNone(self.updater.release)
        self.assertFalse(self.updater.lock.locked())

    def test_prerelease_draft_and_missing_latest_marker_are_rejected(self):
        for label in ('Pre-release','Draft',''):
            self.label=label
            with self.subTest(label=label),self.assertRaises(ValueError):self.check()
        with self.assertRaises(ValueError):
            parse_latest_page(page(fragment='https://github.com/other/repo/releases/expanded_assets/'+TAG),BASE+'/releases/tag/'+TAG)

    def test_missing_wrong_or_ambiguous_asset_digest_is_rejected(self):
        cases=[b'<ul></ul>',asset_row(name='other.zip'),asset_row(digest='sha256:bad'),asset_row()+asset_row(),f'<li><a href="/{REPOSITORY}/releases/download/{TAG}/{PACKAGE_NAME}">zip</a></li>'.encode()+asset_row(name='other.zip')]
        for raw in cases:
            self.asset_override=raw
            with self.subTest(raw=raw),self.assertRaises(ValueError):self.check()
            self.assertIsNone(self.updater.release)
        self.assertFalse(any(method=='HEAD' for method,_ in self.requests))

    def test_invalid_size_or_html_head_response_cannot_offer_an_update(self):
        for length in ('','0','-1','unknown',str(updates.MAX_PACKAGE_BYTES+1)):
            self.head_headers['Content-Length']=length
            with self.subTest(length=length),self.assertRaises(ValueError):self.check()
        self.head_headers={'Content-Length':'3','Content-Type':'text/html'}
        with self.assertRaises(ValueError):self.check()
        self.head_headers={'Content-Length':'3'};self.head_status=206
        with self.assertRaises(ValueError):self.check()

    def test_page_failure_is_chinese_retryable_ui_message_and_not_latest(self):
        self.public_error=HTTPError(BASE+'/releases/latest',403,'Forbidden',{},None)
        with self.assertRaisesRegex(ValueError,'公开发布页也未能完成检查') as failure:self.check()
        self.assertNotIn('HTTP Error',str(failure.exception))
        self.assertIsNone(self.updater.release)
        self.assertFalse(self.updater.lock.locked())

    def test_invalid_encoding_is_reported_without_raw_decode_details(self):
        self.page_override=b'\xff'
        with self.assertRaisesRegex(ValueError,'内容无法读取'):self.check()

    def test_public_check_retries_connection_errors_and_preserves_proxy_fallback(self):
        original=self.respond
        interrupted=False
        def respond(request,**options):
            nonlocal interrupted
            if request.full_url==BASE+'/releases/latest' and not interrupted:
                interrupted=True
                raise URLError(ConnectionResetError('simulated connection reset'))
            return original(request,**options)
        with patch.object(self.updater.opener,'open',side_effect=respond),patch.object(self.updater.direct_opener,'open',side_effect=respond) as direct:
            self.assertTrue(self.check()['available'])
        self.assertEqual(direct.call_count,1)

    def test_fallback_digest_is_still_enforced_by_archive_download(self):
        release=self.check()
        self.assertTrue(release['available'])
        with patch.object(self.updater.opener,'open',return_value=Response(b'bad',release['url'])):
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                self.updater._download_archive(self.root/'test-package.zip')

    def test_body_words_do_not_make_a_formal_release_prerelease_and_links_are_safe(self):
        raw=page(body=f'<h2>v{NEXT_VERSION} · 本次更新</h2><p>Pre-release 字样只是说明。</p><p><a href="javascript:alert(1)">安全文本</a></p><p><a href="https://example.invalid/docs">使用说明</a></p>')
        release=parse_latest_page(raw,BASE+'/releases/tag/'+TAG)
        self.assertNotIn('javascript:',release['body'])
        self.assertIn('[使用说明](https://example.invalid/docs)',release['body'])
        self.assertEqual(parse_package_asset(asset_row(),TAG)['digest'],self.digest)


if __name__=='__main__':unittest.main()
