"""Optional independent HTTP/Chromium analytics controls; no user database is touched."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import html
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8068)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = f'http://127.0.0.1:{args.port}'
    args.artifacts.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cohort, old = today-timedelta(days=2), today-timedelta(days=60)
    deals = [dict(owner='manager', stage=stage, amount=amount, date=cohort.isoformat()) for stage, amount in
             [('new','10.00'),('contact','20.00'),('proposal','30.00'),('won','0.01'),('won','0.02'),('won','9999999999.99'),('lost','40.00'),('lost','50.00')]]
    deals += [dict(owner='manager', stage='won', amount='2.00', date=old.isoformat()),
              dict(owner='other', stage='won', amount='777.77', date=cohort.isoformat()),
              dict(owner='other', stage='lost', amount='33.00', date=cohort.isoformat()),
              dict(owner=None, stage='won', amount='1.11', date=cohort.isoformat())]
    events = [dict(owner='manager', kind=kind, date=cohort.isoformat()) for kind in ['call','call','meeting','email','email','email','comment']]
    events += [dict(owner='manager', kind='comment', date=old.isoformat())]
    events += [dict(owner='other', kind=kind, date=cohort.isoformat()) for kind in ['call','meeting','email','comment']]

    # Control arithmetic uses fixture strings/integers, never the application's analytics functions.
    def expected(actor, recent=False):
        chosen = [d for d in deals if (actor=='admin' or d['owner']==actor) and (not recent or d['date']==cohort.isoformat())]
        history = [e for e in events if (actor=='admin' or e['owner']==actor) and (not recent or e['date']==cohort.isoformat())]
        counts = {stage:sum(d['stage']==stage for d in chosen) for stage in ['new','contact','proposal','won','lost']}
        closed = counts['won']+counts['lost']
        return dict(total=len(chosen), counts=counts, open=len(chosen)-closed, closed=closed,
                    conversion=None if not closed else (Decimal(counts['won'])*100/Decimal(closed)).quantize(Decimal('0.1'),rounding=ROUND_HALF_UP),
                    amount=sum((Decimal(d['amount']) for d in chosen if d['stage']=='won'),Decimal('0.00')),
                    events={kind:sum(e['kind']==kind for e in history) for kind in ['call','meeting','email','comment']})

    with tempfile.TemporaryDirectory(prefix='crm-analytics-check-') as directory:
        db = Path(directory)/'fictional.sqlite3'
        Path(directory,'check_settings.py').write_text('from config.settings import *\nDATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":'+repr(str(db))+'}}\n')
        env = dict(os.environ,DJANGO_SETTINGS_MODULE='check_settings',DJANGO_SECRET_KEY=secrets.token_urlsafe(48),PYTHONPATH=directory+os.pathsep+str(root))

        def manage(*command,input=None):
            subprocess.run([args.python,'manage.py',*command],cwd=root,env=env,input=input,text=True,check=True,stdout=subprocess.DEVNULL)

        def sql(query,params=()):
            with sqlite3.connect(db) as connection:return connection.execute(query,params).fetchall()

        manage('migrate','--noinput')
        manage('shell',input=(
            'from django.contrib.auth.models import User\nfrom crm.models import Client,ClientRequest,Task,Deal,ClientInteraction\n'
            'from decimal import Decimal\nfrom datetime import datetime,timedelta,timezone\nimport json\n'
            f'password={password!r}\n'
            "users={name:User.objects.create_user('analytics-'+name,password=password,is_superuser=(name=='admin')) for name in ['admin','manager','other']}\n"
            "client=Client.objects.create(full_name='Вымышленный клиент аналитики')\n"
            f'fixtures=json.loads({json.dumps(deals)!r})\n'
            "for row in fixtures:\n d=Deal.objects.create(client=client,title='Контрольная сделка',amount=Decimal(row['amount']),stage=row['stage'],assigned_to=users.get(row['owner']));Deal.objects.filter(pk=d.pk).update(created_at=datetime.fromisoformat(row['date']))\n"
            f'fixtures=json.loads({json.dumps(events)!r})\n'
            "for row in fixtures:\n ClientInteraction.objects.create(client=client,created_by=users[row['owner']],interaction_type=row['kind'],subject='Контрольная запись',occurred_at=datetime.fromisoformat(row['date']))\n"
            "now=datetime.now(timezone.utc)\n"
            "own=ClientRequest.objects.create(client=client,title='Своя просроченная',assigned_to=users['manager'],due_at=now-timedelta(days=2))\n"
            "other=ClientRequest.objects.create(client=client,title='Чужая просроченная',assigned_to=users['other'],due_at=now-timedelta(days=2))\n"
            "for status,due in [('new',now-timedelta(days=1)),('completed',now-timedelta(days=1)),('new',now+timedelta(days=2))]:\n Task.objects.create(request=own,title='Контрольная задача',assigned_to=users['manager'],status=status,due_at=due)\n"
            "Task.objects.create(request=other,title='Рассогласованная задача',assigned_to=users['manager'],due_at=now-timedelta(days=1))\n"
            "Task.objects.create(request=other,title='Чужая задача',assigned_to=users['other'],due_at=now-timedelta(days=1))\n"))
        ids = dict(sql('SELECT username,id FROM auth_user'))
        deal_id = sql('SELECT id FROM crm_deal WHERE assigned_to_id=? ORDER BY id LIMIT 1',(ids['analytics-manager'],))[0][0]
        schema = sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
        snapshot = {table:sql('SELECT * FROM '+table+' ORDER BY id') for table in ['crm_client','crm_clientrequest','crm_task','crm_deal','crm_clientinteraction']}

        def csrf(body):return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',body)[1]

        def authenticated(actor):
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def request(path,data=None):
                try:
                    with opener.open(base+path,None if data is None else urllib.parse.urlencode(data).encode()) as response:
                        return response.status,response.url,response.read().decode()
                except urllib.error.HTTPError as error:return error.code,error.url,error.read().decode()
            body=request('/login/')[2]
            if actor:assert request('/login/',{'username':'analytics-'+actor,'password':password,'csrfmiddlewaretoken':csrf(body)})[1]==base+'/'
            return request

        def metric(body,identifier):
            raw=re.search(r'id="'+re.escape(identifier)+r'"[^>]*>(.*?)</[^>]+>',body,re.S)[1]
            return html.unescape(re.sub('<[^>]*>','',raw)).strip()

        def number(value):return Decimal(value.replace('₽','').replace('%','').replace('\xa0','').replace(' ','').replace(',','.'))

        def verify(body,actor,recent=False):
            values=expected(actor,recent)
            for key in ['total','open','closed']:assert int(metric(body,'analytics-deal-'+key))==values[key]
            for stage,count in values['counts'].items():assert int(metric(body,'analytics-stage-'+stage))==count
            assert number(metric(body,'analytics-won-amount'))==values['amount']
            assert number(metric(body,'analytics-conversion'))==values['conversion']
            assert int(metric(body,'analytics-interaction-total'))==sum(values['events'].values())
            for kind,count in values['events'].items():assert int(metric(body,'analytics-type-'+kind))==count
            requests,tasks,late_requests,late_tasks={'admin':(2,5,2,3),'manager':(1,3,1,1),'other':(1,1,1,1)}[actor]
            for name,value in [('request-total',requests),('task-total',tasks),('request-overdue',late_requests),('task-overdue',late_tasks)]:
                assert int(metric(body,'analytics-'+name))==value

        with (args.artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([args.python,'manage.py','runserver',f'127.0.0.1:{args.port}','--noreload'],cwd=root,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(base+'/login/',timeout=1).close();break
                    except urllib.error.URLError:time.sleep(.1)
                else:raise RuntimeError('Server did not start')
                clients={actor:authenticated(actor) for actor in ['admin','manager','other']}
                assert '/login/?next=' in authenticated(None)('/analytics/')[1]
                dates={'period':'custom','start_date':cohort.date().isoformat(),'end_date':(cohort+timedelta(days=1)).date().isoformat()}
                custom='/analytics/?'+urllib.parse.urlencode(dates)
                for actor,request in clients.items():
                    for path,recent in [('/analytics/',False),('/analytics/?period=30d',True),(custom,True)]:
                        status,url,body=request(path);assert status==200;verify(body,actor,recent)
                    bad=request('/analytics/?period=custom&start_date=bad&end_date=2026-01-01')
                    assert bad[0]==200 and 'Показатели за период не рассчитаны' in bad[2] and 'id="analytics-deal-total"' not in bad[2]
                # Financial failures are real SQLite/Django conversions, not synthetic HTTP responses.
                manage('shell',input="from crm.models import Deal,Client\nfrom django.contrib.auth.models import User\nfrom decimal import Decimal\nDeal.objects.create(client=Client.objects.first(),assigned_to=User.objects.get(username='analytics-other'),title='Переполнение контроля',stage='won',amount=Decimal('10000000000.00'))\n")
                verify(clients['manager']('/analytics/')[2],'manager')
                for actor in ['admin','other']:
                    status,url,body=clients[actor]('/analytics/')
                    assert status==200 and 'не может быть достоверно вычислен' in metric(body,'analytics-won-amount')
                    assert 'InvalidOperation' not in body and 'Traceback' not in body
                sql("DELETE FROM crm_deal WHERE title='Переполнение контроля'")
                assert all(sql('SELECT * FROM '+table+' ORDER BY id')==rows for table,rows in snapshot.items())
                print('HTTP admin/two managers/all/30d/custom: independent fixture counts, Decimal totals/conversion, current workload and bad dates OK. Scoped real overflow returns 200/unavailable, no false sum or private details.',flush=True)
                controls={actor:{'all':str(expected(actor)['amount']),'cohort':str(expected(actor,True)['amount']),'cohort_conversion':str(expected(actor,True)['conversion'])} for actor in clients}
                (args.artifacts/'controls.json').write_text(json.dumps(controls,ensure_ascii=False,indent=2)+'\n')
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(executable_path=args.chromium,args=['--no-sandbox'])
                    errors,failures=[],[]
                    def watch(page):
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.on('response',lambda response:failures.append(response.status) if response.status>=500 else None)
                    def login(page):
                        page.goto(base+'/login/');page.get_by_label('Логин').fill('analytics-manager');page.get_by_label('Пароль').fill(password)
                        page.get_by_role('button',name='Войти',exact=True).click();page.wait_for_url(base+'/')
                    for width in [1440,1280,768,375]:
                        context=browser.new_context(viewport={'width':width,'height':1000},java_script_enabled=False,locale='ru-RU')
                        page=context.new_page();watch(page);login(page)
                        page.get_by_role('link',name='Аналитика',exact=True).click();verify(page.content(),'manager')
                        page.get_by_label(re.compile('^Период:?$')).select_option('custom')
                        page.get_by_label(re.compile('^Начальная дата UTC:?$')).fill(dates['start_date'])
                        page.get_by_label(re.compile('^Конечная дата UTC:?$')).fill(dates['end_date'])
                        page.get_by_role('button',name='Рассчитать',exact=True).focus()
                        with page.expect_navigation():page.keyboard.press('Enter')
                        verify(page.content(),'manager',True)
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                        page.screenshot(path=str(args.artifacts/f'analytics-{width}.png'),full_page=True)
                        page.goto(base+'/analytics/?period=custom&start_date=2026-10-03&end_date=2026-10-01')
                        assert page.locator('.errorlist').count()>0
                        assert page.locator('#analytics-deal-total').count()==0
                        assert page.locator('#analytics-task-overdue').inner_text()=='1'
                        page.screenshot(path=str(args.artifacts/f'filter-error-{width}.png'),full_page=True)
                        context.close()
                        print(f'Chromium {width}px JS off: period form/keyboard, scoped controls, date errors, current workload/no overflow OK.',flush=True)
                    for javascript in [False,True]:
                        context=browser.new_context(viewport={'width':1280,'height':1000},java_script_enabled=javascript)
                        page=context.new_page();watch(page);login(page);page.goto(base+'/deals/kanban/')
                        fresh=context.new_page();watch(fresh);fresh.goto(base+'/deals/kanban/')
                        card=fresh.locator(f'[data-deal-id="{deal_id}"]');card.locator('select').select_option('contact')
                        with fresh.expect_navigation():card.locator('button').click()
                        fresh.close();card=page.locator(f'[data-deal-id="{deal_id}"]')
                        if javascript:
                            with page.expect_response(lambda r:r.url.endswith('/change-stage/') and r.status==409):card.locator('.kanban-drag-handle').drag_to(page.locator('[data-stage=contact] h2'))
                            page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('изменена после')")
                        else:
                            card.locator('select').select_option('contact')
                            with page.expect_navigation() as navigation:card.locator('button').click()
                            assert navigation.value.status==409
                        page.get_by_role('link',name='Обновить доску',exact=True).click()
                        card=page.locator(f'[data-deal-id="{deal_id}"]')
                        if javascript:
                            with page.expect_response(lambda r:r.url.endswith('/change-stage/') and r.status==200):card.locator('.kanban-drag-handle').drag_to(page.locator('[data-stage=new] h2'))
                            page.wait_for_function("!document.querySelector('.kanban-scroll').hasAttribute('aria-busy')")
                        else:
                            card.locator('select').select_option('new')
                            with page.expect_navigation():card.locator('button').click()
                        assert sql('SELECT stage FROM crm_deal WHERE id=?',[deal_id])[0][0]=='new'
                        context.close();print(f'Kanban JS={javascript}: actual stale 409, refresh and successful retry OK.',flush=True)
                    browser.close();assert not errors and not failures,(errors,failures)
                assert schema==sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
                print('No pageerror/HTTP 5xx; schema unchanged; fictional database/server removed.',flush=True)
            finally:
                server.terminate();server.wait(timeout=10)


if __name__=='__main__':main()
