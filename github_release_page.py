"""Read official GitHub release HTML without accepting scripts or foreign assets."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import re

from client_version import PACKAGE_NAME, REPOSITORY

VOID_TAGS = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}


class ReleasePage(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url=url
        self.parts=[]
        self.body_depth=0
        self.skipped=0
        self.links=[]
        self.label_depth=0
        self.label=[]
        self.labels=[]
        self.fragments=[]
        self.published_at=None

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if not self.body_depth:
            if tag=='include-fragment' and '/releases/expanded_assets/' in attrs.get('src',''):
                self.fragments.append(urljoin(self.url,attrs['src']))
            if tag=='relative-time' and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ',attrs.get('datetime','')):
                self.published_at=attrs['datetime']
            if self.label_depth:
                if tag not in VOID_TAGS:self.label_depth+=1
            elif tag=='span' and 'Label' in attrs.get('class','').split():
                self.label_depth=1
                self.label=[]
            if tag=='div' and attrs.get('data-test-selector')=='body-content':self.body_depth=1
            return
        if tag not in VOID_TAGS:self.body_depth+=1
        if self.skipped:
            if tag not in VOID_TAGS:self.skipped+=1
            return
        if tag in {'script','style','svg'}:
            self.skipped=1
            return
        if tag in {'p','ul','ol','blockquote'}:self.parts.append('\n\n')
        elif re.fullmatch(r'h[1-6]',tag):self.parts.append('\n\n'+'#'*int(tag[1])+' ')
        elif tag=='li':self.parts.append('\n- ')
        elif tag=='br':self.parts.append('\n')
        elif tag in {'strong','b'}:self.parts.append('**')
        elif tag in {'em','i'}:self.parts.append('*')
        elif tag=='code':self.parts.append('`')
        elif tag=='pre':self.parts.append('\n```\n')
        elif tag=='a':
            href=urljoin(self.url,attrs.get('href',''))
            parsed=urlparse(href)
            allowed=href if parsed.scheme in {'http','https'} and not parsed.username and not parsed.password else ''
            self.links.append(allowed)
            if allowed:self.parts.append('[')
        elif tag=='img':self.parts.append(attrs.get('alt',''))

    def handle_endtag(self, tag):
        if not self.body_depth:
            if self.label_depth and tag not in VOID_TAGS:
                self.label_depth-=1
                if not self.label_depth:self.labels.append(''.join(self.label).strip().lower())
            return
        if self.skipped:
            if tag not in VOID_TAGS:self.skipped-=1
        else:
            if tag in {'strong','b'}:self.parts.append('**')
            elif tag in {'em','i'}:self.parts.append('*')
            elif tag=='code':self.parts.append('`')
            elif tag=='pre':self.parts.append('\n```\n')
            elif tag in {'p','ul','ol','blockquote'} or re.fullmatch(r'h[1-6]',tag):self.parts.append('\n\n')
            elif tag=='a' and self.links:
                href=self.links.pop()
                if href:self.parts.append(']('+href+')')
        if tag not in VOID_TAGS:self.body_depth-=1

    def handle_data(self, data):
        if self.body_depth and not self.skipped:self.parts.append(data)
        elif self.label_depth:self.label.append(data)


def parse_latest_page(raw, url):
    parsed=urlparse(url)
    match=re.fullmatch('/'+re.escape(REPOSITORY)+r'/releases/tag/(v\d+\.\d+\.\d+)',parsed.path)
    if parsed.scheme!='https' or parsed.netloc!='github.com' or parsed.query or parsed.fragment or not match:
        raise ValueError('公开发布页没有返回本项目的正式版本，请打开发布页手动核对。')
    tag=match[1]
    page=ReleasePage(url)
    page.feed(raw.decode('utf-8'))
    expanded=f'https://github.com/{REPOSITORY}/releases/expanded_assets/{tag}'
    if 'latest' not in page.labels or {'pre-release','prerelease','draft'} & set(page.labels) or page.fragments!=[expanded]:
        raise ValueError('无法核对正式最新版或其附件列表，请打开发布页手动下载。')
    notes=re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+','\n\n',''.join(page.parts)).strip()
    return {'tag_name':tag,'body':notes or '请查看 GitHub 发布页中的本次更新说明。',
            'published_at':page.published_at,'draft':False,'prerelease':False,'assets':[]}


class ReleaseAssets(HTMLParser):
    def __init__(self, expected):
        super().__init__(convert_charrefs=True)
        self.expected=expected
        self.items=[]
        self.rows=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='li':self.rows.append({'links':[],'digests':[]})
        if not self.rows:return
        if tag=='a':self.rows[-1]['links'].append(attrs.get('href',''))
        if tag=='clipboard-copy' and re.fullmatch(r'sha256:[0-9a-fA-F]{64}',attrs.get('value','')):
            self.rows[-1]['digests'].append(attrs['value'].lower())

    def handle_endtag(self, tag):
        if tag=='li' and self.rows:
            row=self.rows.pop()
            if self.expected in row['links']:self.items.append(row)


def parse_package_asset(raw, tag):
    path=f'/{REPOSITORY}/releases/download/{tag}/{PACKAGE_NAME}'
    assets=ReleaseAssets(path)
    assets.feed(raw.decode('utf-8'))
    if len(assets.items)!=1 or len(assets.items[0]['digests'])!=1 or assets.items[0]['links'].count(path)!=1:
        raise ValueError('发布页缺少完整客户端或唯一的 GitHub SHA-256 校验值，请打开发布页手动核对。')
    return {'name':PACKAGE_NAME,'browser_download_url':'https://github.com'+path,'digest':assets.items[0]['digests'][0]}
