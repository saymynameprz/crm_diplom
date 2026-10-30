"""Stage 6C HTTP/Chromium checks. Requires optional Playwright and Chromium, not app dependencies."""
import argparse
import sys
import http.cookiejar
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from playwright.sync_api import sync_playwright

args=argparse.ArgumentParser()
args.add_argument('--python', default=sys.executable)
args.add_argument('--chromium', default='/usr/bin/chromium')
args.add_argument('--port', type=int, default=8063)
args.add_argument('--artifacts', type=Path, required=True)
options=args.parse_args()
root=str(Path(__file__).resolve().parents[1]);python=options.python;base=f'http://127.0.0.1:{options.port}'
artifacts=options.artifacts;artifacts.mkdir(parents=True,exist_ok=True)
class DocumentCheck(HTMLParser):
    def __init__(self):super().__init__();self.ids=[];self.labels=[];self.controls=set();self.forms=0
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if 'id' in a:self.ids.append(a['id'])
        if tag=='label' and 'for' in a:self.labels.append(a['for'])
        if tag in ('input','select','textarea') and 'id' in a:self.controls.add(a['id'])
        if tag=='form' and a.get('action','').endswith('/change-stage/'):self.forms+=1
    def validate(self):
        assert len(self.ids)==len(set(self.ids)), 'duplicate element IDs'
        assert all(label in self.controls for label in self.labels), 'unbound label'
with tempfile.TemporaryDirectory(prefix='crm-kanban-validation-') as directory:
    db=directory+'/test.sqlite3'
    Path(directory,'validation_settings.py').write_text('from config.settings import *\nDATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":'+repr(db)+'}}\n')
    env=dict(os.environ,DJANGO_SETTINGS_MODULE='validation_settings',DJANGO_SECRET_KEY=secrets.token_urlsafe(48),PYTHONPATH=directory+':'+root)
    password=secrets.token_urlsafe(24)
    def manage(*args,input=None):subprocess.run([python,'manage.py',*args],cwd=root,env=env,input=input,text=True,check=True,stdout=subprocess.DEVNULL)
    def sql(query,params=()):
        with sqlite3.connect(db) as conn:return conn.execute(query,params).fetchall()
    manage('migrate','--noinput')
    manage('shell',input=('from django.contrib.auth.models import User\nfrom crm.models import Client,Deal\nfrom decimal import Decimal\n'
        f'User.objects.create_superuser("http-admin",password={password!r})\n'
        f'owner=User.objects.create_user("http-manager",password={password!r})\n'
        f'other=User.objects.create_user("http-other",password={password!r})\n'
        'client=Client.objects.create(full_name="Вымышленный клиент длинное название "*4)\n'
        'for n in range(26):\n Deal.objects.create(client=client,title=f"Учебная сделка {n} "+"длинное название "*8,amount=Decimal("1234.56"),assigned_to=owner,expected_close_date="2028-01-15")\n'
        'Deal.objects.create(client=client,title="Чужая карточка",amount=Decimal("99.01"),assigned_to=other)\n'
        'Deal.objects.create(client=client,title="Без ответственного",amount=Decimal("1.00"),stage="won")\n'))
    users=dict(sql('SELECT username,id FROM auth_user'));contact=sql('SELECT id FROM crm_client')[0][0]
    own_id=sql('SELECT id FROM crm_deal WHERE assigned_to_id=? ORDER BY id DESC',(users['http-manager'],))[0][0]
    foreign_id=sql('SELECT id FROM crm_deal WHERE assigned_to_id=?',(users['http-other'],))[0][0]
    schema=sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
    before=sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]
    def token(body):return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',body)[1]
    def version(body,pk):
        content=re.search(r'<article[^>]+data-deal-id="'+str(pk)+r'"[^>]*>(.*?)</article>',body,re.S)[1]
        return re.search(r'name="expected_updated_at" value="([^"]+)"',content)[1]
    def columns(body):
        return {stage:(int(re.search(r'class="column-count">Сделок: (\d+)',content)[1]), re.findall(r'data-deal-id="(\d+)"',content)) for stage,content in re.findall(r'<section[^>]+data-stage="([^"]+)"[^>]*>(.*?)</section>',body,re.S)}
    with open(artifacts/'server.log','w') as log:
        server=subprocess.Popen([python,'manage.py','runserver',f'127.0.0.1:{options.port}','--noreload'],cwd=root,env=env,stdout=log,stderr=log)
        try:
            for _ in range(100):
                try:urllib.request.urlopen(base+'/login/',timeout=1).close();break
                except urllib.error.URLError:time.sleep(.1)
            else:raise RuntimeError('Server not ready')
            def authenticated(username):
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                def request(path,data=None,headers=None):
                    try:
                        with opener.open(urllib.request.Request(base+path,None if data is None else urllib.parse.urlencode(data).encode(),headers=headers or {})) as r:return r.status,r.url,r.read().decode()
                    except urllib.error.HTTPError as e:return e.code,e.url,e.read().decode()
                body=request('/login/')[2]
                if username:request('/login/',{'username':username,'password':password,'csrfmiddlewaretoken':token(body)})
                return request
            manager=authenticated('http-manager');other=authenticated('http-other');admin=authenticated('http-admin')
            body=manager('/deals/kanban/')[2];board=columns(body)
            assert list(board)==['new','contact','proposal','won','lost']
            assert board['new'][0]==26 and len(board['new'][1])==20
            assert len(set(i for c in board.values() for i in c[1]))==20
            assert 'Чужая карточка' not in body and 'Без ответственного' not in body
            assert 'Посмотреть все' in body and 'В этом этапе сделок нет.' in body
            parser=DocumentCheck();parser.feed(body);parser.validate();assert parser.forms==20
            admin_board=columns(admin('/deals/kanban/')[2]);assert admin_board['new'][0]==27 and admin_board['won'][0]==1
            path=f'/deals/{own_id}/change-stage/'
            assert manager(path)[0]==405
            assert manager(path,{'stage':'won'})[0]==403
            assert manager(path,{'stage':'invalid','expected_updated_at':version(body,own_id),'csrfmiddlewaretoken':token(body)})[0]==400
            assert manager(path,{'expected_updated_at':version(body,own_id),'csrfmiddlewaretoken':token(body)})[0]==400
            assert sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]==before
            assert other(path,{'stage':'won','csrfmiddlewaretoken':token(other('/deals/kanban/')[2])})[0]==404
            assert manager(f'/deals/{foreign_id}/change-stage/',{'stage':'won','csrfmiddlewaretoken':token(body)})[0]==404
            assert admin('/deals/999999/change-stage/',{'stage':'won','csrfmiddlewaretoken':token(admin('/deals/kanban/')[2])})[0]==404
            for stage in ('won','contact','proposal','lost','new'):
                status,url,body=manager(path,{'stage':stage,'expected_updated_at':version(body,own_id),'csrfmiddlewaretoken':token(body),'amount':'-1','assigned_to':users['http-other'],'client':999999,'title':'Подмена','next':'https://untrusted.example/'})
                assert status==200 and url==base+'/deals/kanban/'
                assert str(own_id) in columns(body)[stage][1]
                assert sql('SELECT stage,assigned_to_id,title,amount,client_id,created_at FROM crm_deal WHERE id=?',(own_id,))[0]==(stage,users['http-manager'],before[1],before[3],contact,before[6])
            same=sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]
            manager(path,{'stage':'new','expected_updated_at':version(body,own_id),'csrfmiddlewaretoken':token(body)})
            assert sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]==same
            edit=admin(f'/deals/{own_id}/edit/')[2]
            admin(f'/deals/{own_id}/edit/',{'client':contact,'title':before[1],'amount':'1234.56','stage':'new','assigned_to':users['http-other'],'csrfmiddlewaretoken':token(edit)})
            assert str(own_id) not in columns(manager('/deals/kanban/')[2])['new'][1]
            assert str(own_id) in columns(other('/deals/kanban/')[2])['new'][1]
            assert manager(path,{'stage':'won','csrfmiddlewaretoken':token(manager('/deals/kanban/')[2])})[0]==404
            assert schema==sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
            # Explicit Accept JSON uses the same endpoint, without redirects.
            body=other('/deals/kanban/')[2]
            jar=http.cookiejar.CookieJar()
            opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login=opener.open(base+'/login/').read().decode()
            opener.open(base+'/login/',urllib.parse.urlencode({'username':'http-other','password':password,'csrfmiddlewaretoken':token(login)}).encode()).close()
            json_board=opener.open(base+'/deals/kanban/').read().decode()
            import json
            req=urllib.request.Request(base+path,data=urllib.parse.urlencode({'stage':'contact','expected_updated_at':version(json_board,own_id)}).encode(),headers={'Accept':'application/json','X-CSRFToken':token(json_board)})
            with opener.open(req) as result:
                data=json.loads(result.read())
                assert result.status==200 and data['success'] and data['stage']=='contact'
                assert {c['stage'] for c in data['columns']}=={'new','contact'}
                assert {c['stage']:c['count'] for c in data['columns']}=={'new':1,'contact':1}
            body=admin('/deals/kanban/')[2]
            admin(path,{'stage':'new','expected_updated_at':version(body,own_id),'csrfmiddlewaretoken':token(body)})
            print('HTTP JSON mode: authenticated request, updated fragments and scoped counts OK.')
            print('HTTP admin/two managers: 5 columns, scoped counts, 20/26 cap, all-stage moves, GET/CSRF/invalid/foreign/missing errors, field forgery, fixed redirect, no-op, reassignment and unchanged schema OK. HTML IDs/labels checked.')
            sql('UPDATE crm_deal SET assigned_to_id=? WHERE id=?',(users['http-manager'],own_id))
            # Real JSON status matrix, including a controlled SQLite failure only in this temporary DB.
            anonymous=authenticated(None)
            def json_move(actor,pk,stage,stamp=None,csrf=True,extra=None):
                body=actor('/deals/kanban/')[2] if actor is not anonymous else actor('/login/')[2]
                payload={'stage':stage,'expected_updated_at':stamp or (version(body,pk) if f'data-deal-id="{pk}"' in body and actor is not anonymous else '2000-01-01T00:00:00+00:00')}
                if extra:payload.update(extra)
                headers={'Accept':'application/json'}
                if csrf:headers['X-CSRFToken']=token(body)
                status,url,result=actor(f'/deals/{pk}/change-stage/',payload,headers)
                if actor in (manager,other,anonymous):assert 'Чужая карточка' not in result and 'Без ответственного' not in result
                parsed=json.loads(result)
                assert 'Traceback' not in result and 'PRIVATE' not in result
                return status,parsed
            assert json_move(manager,own_id,'invalid')[0]==400
            assert json_move(anonymous,own_id,'won')[0]==401
            assert json_move(manager,own_id,'won',csrf=False)[0]==403
            assert json_move(other,own_id,'won')[0]==404
            assert json_move(admin,999999,'won')[0]==404
            assert manager(path,headers={'Accept':'application/json'})[0]==405
            stale=version(manager('/deals/kanban/')[2],own_id)
            assert json_move(manager,own_id,'contact',stamp=stale)[0]==200
            snapshot=sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]
            assert json_move(manager,own_id,'won',stamp=stale)[0]==409
            assert sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]==snapshot
            html=manager(path,{'stage':'won','expected_updated_at':stale,'csrfmiddlewaretoken':token(manager('/deals/kanban/')[2])})
            assert html[0]==409 and 'Обновить доску' in html[2]
            sql("CREATE TRIGGER controlled_failure BEFORE UPDATE OF stage ON crm_deal WHEN NEW.id="+str(own_id)+" BEGIN SELECT RAISE(FAIL, 'PRIVATE validation failure'); END")
            try:assert json_move(manager,own_id,'lost')[0]==500
            finally:sql('DROP TRIGGER controlled_failure')
            assert sql('SELECT * FROM crm_deal WHERE id=?',(own_id,))[0]==snapshot
            assert json_move(manager,own_id,'new')[0]==200
            assert schema==sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
            print('Real HTTP JSON 400/401/403/404/405/409/500, HTML 409, CSRF, no mutation on conflict/failure and no private details OK.')
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=options.chromium,args=['--no-sandbox'])
                for width in (1440,1280,768,375):
                    page=browser.new_page(viewport={'width':width,'height':1000},java_script_enabled=False)
                    errors=[];failures=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('response',lambda r:failures.append(r.status) if r.status>=500 else None)
                    page.goto(base+'/login/');page.get_by_label('Логин').fill('http-admin');page.get_by_label('Пароль').fill(password)
                    page.get_by_role('button',name='Войти',exact=True).click();page.wait_for_url(base+'/')
                    if page.locator('.mobile-navigation').is_visible():page.locator('.mobile-navigation summary').click()
                    page.get_by_role('link',name='Сделки',exact=True).click();page.get_by_role('link',name='Воронка продаж',exact=True).click()
                    assert page.locator('.kanban-column').count()==5
                    assert page.locator('[data-stage=new] .kanban-deal').count()==20
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    region=page.get_by_role('region',name='Доска сделок',exact=True)
                    # Available board width now excludes the persistent sidebar.
                    if region.evaluate('(e)=>e.scrollWidth > e.clientWidth+1'):
                        region.focus();page.keyboard.press('ArrowRight');page.wait_for_timeout(200)
                        assert region.evaluate('(e)=>e.scrollLeft')>0
                    page.screenshot(path=str(artifacts/f'board-{width}.png'),full_page=True)
                    last=page.locator('[data-stage=lost]')
                    last.scroll_into_view_if_needed()
                    box=last.bounding_box();assert box['x']>=0 and box['x']+box['width']<=width
                    page.screenshot(path=str(artifacts/f'last-column-{width}.png'),full_page=True)
                    card=page.locator(f'[data-deal-id="{own_id}"]')
                    card.scroll_into_view_if_needed();select=card.get_by_label(f'Этап сделки №{own_id}',exact=True)
                    select.focus();page.keyboard.press('End');assert select.input_value()=='lost'
                    page.keyboard.press('Tab')
                    assert card.get_by_role('button',name=f'Переместить сделку №{own_id}',exact=True).evaluate('(e)=>e===document.activeElement')
                    with page.expect_navigation():page.keyboard.press('Enter')
                    assert page.locator(f'[data-stage=lost] [data-deal-id="{own_id}"]').count()==1
                    card=page.locator(f'[data-stage=lost] [data-deal-id="{own_id}"]');card.scroll_into_view_if_needed()
                    page.screenshot(path=str(artifacts/f'moved-{width}.png'),full_page=True)
                    # All five stages through normal forms with fresh versions from each rendered DOM.
                    for stage in ('contact','proposal','won','lost','new'):
                        form=page.locator(f'[data-deal-id="{own_id}"] form')
                        form.locator('select').select_option(stage)
                        with page.expect_navigation():form.locator('button').click()
                        assert page.locator(f'[data-stage={stage}] [data-deal-id="{own_id}"]').count()==1
                        assert page.locator('.kanban-column').evaluate_all('(cols)=>cols.every(c=>c.querySelectorAll(".kanban-deal").length<=20)')
                    # A real competing HTTP writer leaves this old HTML form stale.
                    assert json_move(admin,own_id,'won')[0]==200
                    form=page.locator(f'[data-deal-id="{own_id}"] form')
                    form.locator('select').select_option('lost')
                    with page.expect_navigation():form.locator('button').click()
                    assert page.locator('h1').inner_text()=='Конфликт изменения этапа'
                    page.screenshot(path=str(artifacts/f'html-conflict-{width}.png'))
                    page.get_by_role('link',name='Обновить доску',exact=True).click()
                    assert page.locator(f'[data-stage=won] [data-deal-id="{own_id}"]').count()==1
                    form=page.locator(f'[data-deal-id="{own_id}"] form')
                    form.locator('select').select_option('new')
                    with page.expect_navigation():form.locator('button').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert not errors and not failures,(errors,failures)
                    page.close();print(f'Chromium {width}px (JS disabled): all stages/HTML conflict/refreshed version/card limit, keyboard form move, last-column reachability, horizontal scroll/no page overflow and no JS/5xx errors OK.')
                # JavaScript-enabled native drag and drop on all four viewport widths.
                for width in (1440,1280,768,375):
                    context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page()
                    errors=[];unexpected_5xx=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('response',lambda r:unexpected_5xx.append(r.status) if r.status>=500 else None)
                    page.goto(base+'/login/');page.get_by_label('Логин').fill('http-admin');page.get_by_label('Пароль').fill(password)
                    page.get_by_role('button',name='Войти',exact=True).click();page.wait_for_url(base+'/')
                    page.goto(base+'/deals/kanban/')
                    page.wait_for_selector('.kanban-drag-handle:not([hidden])')
                    navigation_count=[];page.on('framenavigated',lambda frame:navigation_count.append(frame.url))
                    page.evaluate('''() => {const b=document.querySelector('.kanban-scroll');window.dragSeen=false;window.dropSeen=false;
                        b.addEventListener('dragstart',()=>window.dragSeen=!!document.querySelector('.is-dragging'));
                        b.addEventListener('dragover',()=>window.dropSeen=!!document.querySelector('.is-drop-target'));}''')
                    def card():return page.locator(f'[data-deal-id="{own_id}"]')
                    def column(stage):return page.locator(f'.kanban-column[data-stage="{stage}"]')
                    def drag(stage):
                        # Put source and destination in view before a native drag; at narrow widths
                        # use browser-native DataTransfer for off-screen-column event delivery.
                        source=card().locator('.kanban-drag-handle')
                        source.scroll_into_view_if_needed()
                        target_visible=column(stage).locator('h2').evaluate('''e=>{
                            const a=e.getBoundingClientRect(),b=e.closest('.kanban-scroll').getBoundingClientRect();
                            return a.left+a.width/2>=b.left && a.left+a.width/2<=b.right;
                        }''')
                        if width>=1280 and target_visible:
                            source.drag_to(column(stage).locator('h2'))
                        else:
                            transfer=page.evaluate_handle('new DataTransfer()')
                            source.dispatch_event('dragstart',{'dataTransfer':transfer})
                            column(stage).scroll_into_view_if_needed()
                            column(stage).dispatch_event('dragover',{'dataTransfer':transfer})
                            column(stage).dispatch_event('drop',{'dataTransfer':transfer})
                            source.dispatch_event('dragend',{'dataTransfer':transfer})
                    def wait_stage(stage):
                        page.wait_for_function("([id,stage])=>!document.querySelector('.kanban-scroll').hasAttribute('aria-busy') && !!document.querySelector(`.kanban-column[data-stage=${stage}] [data-deal-id='${id}']`)",arg=[str(own_id),stage])
                    for stage in ('contact','proposal','won','new','lost','won','contact'):
                        drag(stage);wait_stage(stage)
                        assert page.locator(f'[data-deal-id="{own_id}"]').count()==1
                        assert page.locator('.kanban-column').evaluate_all('(cols)=>cols.every(c=>c.querySelectorAll(".kanban-deal").length<=20)')
                    assert not navigation_count,navigation_count
                    assert page.evaluate('window.dragSeen && window.dropSeen')
                    # Two real tabs: one saves, the old board must receive 409 and keep its DOM.
                    stale_version=card().locator('[name=expected_updated_at]').input_value()
                    fresh=page.context.new_page();fresh.goto(base+'/deals/kanban/')
                    fresh_card=fresh.locator(f'[data-deal-id="{own_id}"]')
                    fresh_card.locator('select').select_option('won')
                    with fresh.expect_navigation():fresh_card.locator('button').click()
                    fresh.close()
                    with page.expect_response(lambda response:response.url.endswith('/change-stage/') and response.status==409):drag('proposal')
                    page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('изменена после')")
                    assert card().locator('xpath=..').get_attribute('data-stage')=='contact'
                    assert card().locator('[name=expected_updated_at]').input_value()==stale_version
                    assert page.locator('#kanban-refresh').is_visible()
                    page.screenshot(path=str(artifacts/f'conflict-{width}.png'))
                    page.get_by_role('link',name='Обновить доску',exact=True).click();page.wait_for_selector('.kanban-drag-handle:not([hidden])')
                    assert card().locator('xpath=..').get_attribute('data-stage')=='won'
                    assert card().locator('[name=expected_updated_at]').input_value()!=stale_version
                    drag('contact');wait_stage('contact')
                    # Saving/busy state and duplicate-drop protection with a deliberately delayed fetch.
                    page.evaluate("""() => { window.realFetch=window.fetch; window.requestCount=0;
                        window.fetch=async (...args)=> {window.requestCount++;await new Promise(r=>setTimeout(r,500));return window.realFetch(...args);}; }""")
                    drag('proposal')
                    page.wait_for_selector('.kanban-scroll[aria-busy=true]')
                    assert 'Сохраняем' in page.locator('#kanban-status').inner_text()
                    assert card().get_attribute('class').find('is-saving')>=0
                    # A second drop while busy has no active drag and must not issue another request.
                    transfer=page.evaluate_handle('new DataTransfer()')
                    column('lost').dispatch_event('drop',{'dataTransfer':transfer})
                    wait_stage('proposal');assert page.evaluate('window.requestCount')==1
                    page.evaluate('() => { window.fetch=window.realFetch; }')
                    # Known JSON errors: UI never moves optimistically and reports them.
                    for code in (400,403,404,409,500):
                        page.route('**/change-stage/',lambda route,request,code=code:route.fulfill(status=code,content_type='application/json',body=json.dumps({'success':False,'message':f'Проверочный отказ {code}'})))
                        drag('lost')
                        page.wait_for_function("!document.querySelector('.kanban-scroll').hasAttribute('aria-busy')")
                        page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('Проверочный отказ')")
                        assert card().locator('xpath=..').get_attribute('data-stage')=='proposal'
                        assert page.locator('.is-drop-target,.is-dragging,.is-saving').count()==0
                        page.unroute('**/change-stage/')
                        page.reload();page.wait_for_selector('.kanban-drag-handle:not([hidden])')
                    unexpected_5xx.clear()  # the synthetic 500 above was deliberately injected
                    # Network interruption may happen before or after the server commits: never claim rollback.
                    page.route('**/change-stage/',lambda route:route.abort('failed'))
                    drag('lost')
                    page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('Сервер мог сохранить')")
                    assert page.locator('#kanban-refresh').is_visible()
                    assert card().locator('xpath=..').get_attribute('data-stage')=='proposal'
                    page.screenshot(path=str(artifacts/f'network-error-{width}.png'),full_page=False)
                    page.unroute('**/change-stage/')
                    page.get_by_role('link',name='Обновить доску',exact=True).click();page.wait_for_selector('.kanban-drag-handle:not([hidden])')
                    drag('lost');wait_stage('lost')
                    # Non-JSON login page is never inserted as a Kanban fragment.
                    page.route('**/change-stage/',lambda route:route.fulfill(status=200,content_type='text/html',body='<h1>LOGIN TEST</h1>'))
                    drag('won');page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('сессия')")
                    assert page.locator('h1').inner_text()=='Воронка продаж'
                    page.unroute('**/change-stage/');page.reload();page.wait_for_selector('.kanban-drag-handle:not([hidden])')
                    if width==1440:
                        # Exercise the real 15-second AbortController timer, with a pending transport.
                        page.evaluate("""() => { window.fetch=(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Timeout','AbortError')))); }""")
                        drag('won');page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('Сервер мог сохранить')",timeout=20000)
                        page.reload();page.wait_for_selector('.kanban-drag-handle:not([hidden])')
                    drag('new');wait_stage('new')
                    # Full column is refilled and all columns agree with SQL for admin.
                    expected=dict(sql('SELECT stage,COUNT(*) FROM crm_deal GROUP BY stage'))
                    for stage in ('new','contact','proposal','won','lost'):
                        assert column(stage).locator('.column-count').inner_text()==f"Сделок: {expected.get(stage,0)}"
                    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                    column('lost').scroll_into_view_if_needed()
                    page.screenshot(path=str(artifacts/f'js-board-{width}.png'),full_page=False)
                    assert not errors and not unexpected_5xx,(errors,unexpected_5xx)
                    context.close();print(f'Chromium JS {width}px: sequential DnD/DOM updates/counts, busy lock, real conflict/reload and 400/403/404/409/500/network/login errors, recovery and no navigation/overflow/uncaught JS OK.')
                browser.close()
        finally:server.terminate();server.wait(timeout=10)
print('Temporary database and all fictional data removed.')
