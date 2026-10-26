"""Optional HTTP/Chromium verification using fictional data and a disposable SQLite DB."""
import argparse
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
    parser.add_argument("--python", default=sys.executable, help="Python in the Django app venv")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--port", type=int, default=8067)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = f"http://127.0.0.1:{args.port}"
    args.artifacts.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)

    with tempfile.TemporaryDirectory(prefix="crm-history-check-") as directory:
        db = Path(directory)/"fictional.sqlite3"
        Path(directory, "history_settings.py").write_text(
            "from config.settings import *\nDATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':"+repr(str(db))+"}}\n")
        env = dict(os.environ, DJANGO_SETTINGS_MODULE="history_settings", DJANGO_SECRET_KEY=secrets.token_urlsafe(48),
                   PYTHONPATH=directory+os.pathsep+str(root))

        def manage(*command, input=None):
            subprocess.run([args.python, "manage.py", *command], cwd=root, env=env, input=input, text=True,
                           check=True, stdout=subprocess.DEVNULL)

        def sql(query, params=()):
            with sqlite3.connect(db) as connection:
                return connection.execute(query, params).fetchall()

        manage("migrate", "--noinput")
        manage("migrate", "crm", "0005_deal", "--noinput")
        manage("shell", input=(
            "from django.contrib.auth.models import User\nfrom crm.models import Client,ClientRequest,Task,Deal,ClientInteraction\n"
            "from decimal import Decimal\nfrom datetime import datetime,timedelta,timezone\n"
            f"admin=User.objects.create_superuser('history-admin',password={password!r})\n"
            f"manager=User.objects.create_user('history-manager',password={password!r})\n"
            f"other=User.objects.create_user('history-other',password={password!r})\n"
            "client=Client.objects.create(full_name='Вымышленный клиент '+ 'длинное имя '*8)\n"
            "second=Client.objects.create(full_name='Второй учебный клиент')\n"
            "request=ClientRequest.objects.create(client=client,title='Сохранить заявку',assigned_to=manager)\n"
            "Task.objects.create(request=request,title='Сохранить задачу',assigned_to=manager)\n"
            "Deal.objects.create(client=client,title='Проверка Kanban 6C',amount=Decimal('0.10'),assigned_to=manager)\n"
        ))
        legacy_tables = ("crm_client", "crm_clientrequest", "crm_task", "crm_deal")
        legacy = {table: sql(f"SELECT * FROM {table} ORDER BY id") for table in legacy_tables}
        with sqlite3.connect(db) as source, sqlite3.connect(Path(directory)/'after6c.sqlite3') as backup:
            source.backup(backup)
        # Upgrade an independent file copy of the filled after-6C fixture.
        with sqlite3.connect(Path(directory)/'after6c.sqlite3') as source, sqlite3.connect(db) as copy:
            source.backup(copy)
        manage("migrate", "--noinput")
        assert all(sql(f"SELECT * FROM {table} ORDER BY id") == rows for table, rows in legacy.items())
        assert sql('SELECT COUNT(*) FROM crm_clientinteraction')[0][0] == 0
        print('Filled after-6C SQLite copy upgrade: clients/requests/tasks/deals unchanged; history table initially empty OK.', flush=True)
        manage("shell", input=(
            "from crm.models import Client,ClientInteraction\nfrom django.contrib.auth.models import User\n"
            "from datetime import datetime,timedelta,timezone\n"
            "client=Client.objects.order_by('pk').first();manager=User.objects.get(username='history-manager');other=User.objects.get(username='history-other')\n"
            "for n in range(13):\n ClientInteraction.objects.create(client=client,created_by=manager,interaction_type='call',"
            "subject=f'Факт {n} '+ 'длинная тема '*10,description='Вымышленная переписка',occurred_at=datetime(2021,1,1,tzinfo=timezone.utc)+timedelta(minutes=n))\n"
            "ClientInteraction.objects.create(client=client,created_by=other,interaction_type='meeting',subject='Скрытая чужая запись',occurred_at=datetime(2021,1,2,tzinfo=timezone.utc))\n"))
        users = dict(sql("SELECT username,id FROM auth_user"))
        client_id, second_id = [row[0] for row in sql("SELECT id FROM crm_client ORDER BY id")]
        own_id = sql("SELECT id FROM crm_clientinteraction WHERE created_by_id=? ORDER BY id DESC", (users['history-manager'],))[0][0]
        foreign_id = sql("SELECT id FROM crm_clientinteraction WHERE created_by_id=?", (users['history-other'],))[0][0]
        deal_id = sql("SELECT id FROM crm_deal")[0][0]
        old_tables = ("crm_client", "crm_clientrequest", "crm_task", "crm_deal")
        before = {table: sql(f"SELECT * FROM {table} ORDER BY id") for table in old_tables}

        def token(body):
            return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)[1]

        def payload(**extra):
            return dict(client=client_id, interaction_type="comment", subject="HTTP учебная заметка",
                        description="Не потерять введённый текст", occurred_at="2020-01-15T12:30", **extra)

        def authenticated(username):
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

            def request(path, data=None, headers=None):
                request = urllib.request.Request(base+path, None if data is None else urllib.parse.urlencode(data).encode(), headers=headers or {})
                try:
                    with opener.open(request) as response:
                        return response.status, response.url, response.read().decode()
                except urllib.error.HTTPError as error:
                    return error.code, error.url, error.read().decode()

            body = request('/login/')[2]
            if username:
                assert request('/login/', dict(username=username, password=password, csrfmiddlewaretoken=token(body)))[1] == base+'/'
            return request

        with (args.artifacts/'server.log').open('w') as log:
            server = subprocess.Popen([args.python, 'manage.py', 'runserver', f'127.0.0.1:{args.port}', '--noreload'],
                                      cwd=root, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(base+'/login/', timeout=1).close()
                        break
                    except urllib.error.URLError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Verification server did not start')

                admin, manager, other, anonymous = [authenticated(name) for name in ('history-admin', 'history-manager', 'history-other', None)]
                create = '/interactions/create/'
                for actor, username in ((admin, 'history-admin'), (manager, 'history-manager')):
                    for kind in ('call', 'meeting', 'email', 'comment'):
                        data = payload(); data.update(interaction_type=kind, created_by=users['history-other'], created_at='2000-01-01', updated_at='2000-01-01')
                        body = actor(create)[2]
                        assert actor(create, data)[0] == 403
                        data['csrfmiddlewaretoken'] = token(body)
                        status, url, body = actor(create, data)
                        assert status == 200 and '/interactions/' in url
                        assert sql('SELECT created_by_id,interaction_type FROM crm_clientinteraction ORDER BY id DESC LIMIT 1')[0] == (users[username], kind)

                for field, value in (('subject', ' '), ('interaction_type', 'invalid'), ('occurred_at', '2099-01-01T12:00'), ('client', 999999)):
                    body = manager(create)[2]
                    data = payload(); data.update({field: value, 'csrfmiddlewaretoken': token(body)})
                    assert 'Не потерять введённый текст' in manager(create, data)[2]

                for actor in (manager, other):
                    stranger = foreign_id if actor is manager else own_id
                    for suffix in ('', 'edit/', 'delete/'):
                        assert actor(f'/interactions/{stranger}/{suffix}')[0] == 404
                    body = actor(create)[2]
                    for suffix in ('edit/', 'delete/'):
                        data = payload(); data['csrfmiddlewaretoken'] = token(body)
                        assert actor(f'/interactions/{stranger}/{suffix}', data)[0] == 404
                assert manager(f'/interactions/{own_id}/delete/')[0] == 403
                assert manager(f'/interactions/{own_id}/delete/', {'csrfmiddlewaretoken': token(manager(create)[2])})[0] == 403
                for path in ('/interactions/', create, f'/interactions/{own_id}/'):
                    assert '/login/?next=' in anonymous(path)[1]
                body = manager('/interactions/?q='+urllib.parse.quote('Скрытая'))[2]
                assert 'Скрытая чужая запись' not in body
                assert 'Скрытая чужая запись' not in manager(f'/clients/{client_id}/')[2]
                assert 'Скрытая чужая запись' in admin(f'/clients/{client_id}/')[2]
                edit = f'/interactions/{foreign_id}/edit/'
                data = payload(); data.update(created_by=users['history-admin'], csrfmiddlewaretoken=token(admin(edit)[2]))
                assert admin(edit, data)[0] == 200
                assert sql('SELECT created_by_id FROM crm_clientinteraction WHERE id=?', (foreign_id,))[0][0] == users['history-other']
                # Restore a distinctive foreign title after the edit check.
                sql('UPDATE crm_clientinteraction SET subject=? WHERE id=?', ('Скрытая чужая запись', foreign_id))
                path = f'/clients/{client_id}/delete/'
                response = admin(path, {'csrfmiddlewaretoken': token(admin(path)[2])})
                assert response[0] == 200 and 'история взаимодействий' in response[2]
                path = f'/interactions/{own_id}/delete/'
                assert admin(path)[0] == 200 and sql('SELECT id FROM crm_clientinteraction WHERE id=?', (own_id,))
                assert admin(path, {'csrfmiddlewaretoken': token(admin(path)[2])})[0] == 200
                assert not sql('SELECT id FROM crm_clientinteraction WHERE id=?', (own_id,))
                assert all(sql(f'SELECT * FROM {table} ORDER BY id') == rows for table, rows in before.items())
                print('HTTP admin/two managers: four types/server authorship, forged metadata, CSRF, validation, scoped URLs/search/client history, protected client, admin edit/delete and unchanged old records OK.', flush=True)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=args.chromium, args=['--no-sandbox'])
                    print('Chromium', browser.version, flush=True)
                    errors, failures = [], []

                    def watch(page):
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('response', lambda response: failures.append(response.status) if response.status >= 500 else None)

                    def login(page, username):
                        page.goto(base+'/login/')
                        page.get_by_label(re.compile(r'^Логин:?$')).fill(username)
                        page.get_by_label(re.compile(r'^Пароль:?$')).fill(password)
                        page.get_by_role('button', name='Войти', exact=True).click()
                        page.wait_for_url(base+'/')

                    def screenshot(page, name):
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), name
                        page.screenshot(path=str(args.artifacts/name), full_page=True)

                    for width in (1440, 1280, 768, 375):
                        context = browser.new_context(viewport={'width': width, 'height': 1000})
                        page = context.new_page(); watch(page); login(page, 'history-manager')
                        page.get_by_role('link', name='История', exact=True).click()
                        page.get_by_label(re.compile(r'^Поиск\ по\ теме\ или\ клиенту:?$')).fill('Факт')
                        page.get_by_label(re.compile(r'^Тип:?$')).select_option('call')
                        page.get_by_label(re.compile(r'^Клиент:?$')).select_option(str(client_id))
                        page.get_by_label(re.compile(r'^Порядок:?$')).select_option('oldest')
                        page.get_by_role('button', name='Применить', exact=True).click()
                        assert page.get_by_role('list', name='Записи взаимодействий').locator('li').count() == 10
                        assert 'interaction_type=call' in page.get_by_role('link', name='Далее', exact=True).get_attribute('href')
                        page.get_by_role('link', name='Далее', exact=True).click()
                        assert page.get_by_role('list', name='Записи взаимодействий').locator('li').count() == 2
                        screenshot(page, f'list-{width}.png')
                        page.goto(base+f'/clients/{client_id}/')
                        history = page.locator('section[aria-labelledby=client-history-title]')
                        assert history.locator('li').count() == 10
                        assert 'Скрытая чужая запись' not in history.inner_text()
                        assert history.get_by_role('link', name='Вся история этого клиента').get_attribute('href') == f'/interactions/?client={client_id}'
                        screenshot(page, f'client-{width}.png')
                        history.get_by_role('link', name='Добавить взаимодействие').click()
                        assert page.get_by_label(re.compile(r'^Клиент:?$')).input_value() == str(client_id)
                        title = (f'Браузер {width} '+ 'длинная тема '*9).strip()
                        page.get_by_label(re.compile(r'^Тип\ взаимодействия:?$')).select_option('email')
                        page.get_by_label(re.compile(r'^Тема:?$')).fill(title)
                        page.get_by_label(re.compile(r'^Описание:?$')).fill('Вымышленная запись <script>test()</script>')
                        page.get_by_label(re.compile(r'^Время\ взаимодействия:?$')).fill('2099-01-15T12:30')
                        page.get_by_role('button', name='Сохранить', exact=True).click()
                        assert 'Будущая дата запрещена' in page.locator('.errorlist').inner_text()
                        assert page.get_by_label(re.compile(r'^Тема:?$')).input_value() == title
                        screenshot(page, f'form-error-{width}.png')
                        page.get_by_label(re.compile(r'^Время\ взаимодействия:?$')).fill('2020-01-15T12:30')
                        page.get_by_role('button', name='Сохранить', exact=True).focus()
                        with page.expect_navigation(): page.keyboard.press('Enter')
                        interaction_id = int(re.search(r'/interactions/(\d+)/', page.url)[1])
                        assert page.locator('h1').inner_text() == title
                        assert page.locator('script').count() == 0
                        assert 'history-manager' in page.locator('dl').inner_text()
                        screenshot(page, f'detail-{width}.png')
                        page.get_by_role('link', name='Редактировать', exact=True).click()
                        page.get_by_label(re.compile(r'^Описание:?$')).fill('Изменено с клавиатуры')
                        page.get_by_role('button', name='Сохранить', exact=True).click()
                        assert 'Изменено с клавиатуры' in page.locator('dl').inner_text()
                        assert sql('SELECT created_by_id FROM crm_clientinteraction WHERE id=?', (interaction_id,))[0][0] == users['history-manager']
                        context.close()
                        context = browser.new_context(viewport={'width': width, 'height': 1000})
                        page = context.new_page(); watch(page); login(page, 'history-admin')
                        page.goto(base+f'/interactions/{interaction_id}/delete/')
                        screenshot(page, f'delete-{width}.png')
                        assert sql('SELECT id FROM crm_clientinteraction WHERE id=?', (interaction_id,))
                        page.get_by_role('link', name='Отмена', exact=True).click()
                        assert sql('SELECT id FROM crm_clientinteraction WHERE id=?', (interaction_id,))
                        page.goto(base+f'/interactions/{interaction_id}/delete/')
                        page.get_by_role('button', name='Удалить', exact=True).click()
                        page.wait_for_url(base+'/interactions/')
                        assert not sql('SELECT id FROM crm_clientinteraction WHERE id=?', (interaction_id,))
                        context.close()
                        print(f'Chromium {width}px history: combined filters/pagination, scoped client top10, create/future error/keyboard/edit, escaped text, admin cancel/delete, long names/no overflow OK.', flush=True)

                    # Short regression of unchanged 6C: HTML and native DnD, two old views, refresh and retry.
                    for javascript in (False, True):
                        context = browser.new_context(viewport={'width': 1280, 'height': 1000}, java_script_enabled=javascript)
                        page = context.new_page(); watch(page); login(page, 'history-manager')
                        page.goto(base+'/deals/kanban/')
                        fresh = context.new_page(); watch(fresh); fresh.goto(base+'/deals/kanban/')
                        current_stage = sql('SELECT stage FROM crm_deal WHERE id=?', (deal_id,))[0][0]
                        target = 'contact' if current_stage == 'new' else 'new'
                        card = fresh.locator(f'[data-deal-id="{deal_id}"]')
                        card.locator('select').select_option(target)
                        with fresh.expect_navigation(): card.locator('button').click()
                        fresh.close()
                        card = page.locator(f'[data-deal-id="{deal_id}"]')
                        if javascript:
                            with page.expect_response(lambda r: r.url.endswith('/change-stage/') and r.status == 409):
                                card.locator('.kanban-drag-handle').drag_to(page.locator(f'[data-stage={target}] h2'))
                            page.wait_for_function("document.querySelector('#kanban-status').textContent.includes('изменена после')")
                            assert card.locator('xpath=..').get_attribute('data-stage') == current_stage
                        else:
                            card.locator('select').select_option(target)
                            with page.expect_navigation() as nav: card.locator('button').click()
                            assert nav.value.status == 409
                        page.get_by_role('link', name='Обновить доску', exact=True).click()
                        assert page.locator(f'[data-stage={target}] [data-deal-id="{deal_id}"]').count() == 1
                        card = page.locator(f'[data-deal-id="{deal_id}"]')
                        if javascript:
                            with page.expect_response(lambda r: r.url.endswith('/change-stage/') and r.status == 200):
                                card.locator('.kanban-drag-handle').drag_to(page.locator(f'[data-stage={current_stage}] h2'))
                            page.wait_for_function("!document.querySelector('.kanban-scroll').hasAttribute('aria-busy')")
                        else:
                            card.locator('select').select_option(current_stage)
                            with page.expect_navigation(): card.locator('button').click()
                        assert sql('SELECT stage FROM crm_deal WHERE id=?', (deal_id,))[0][0] == current_stage
                        screenshot(page, f'kanban-js-{javascript}.png')
                        context.close()
                        print(f'Kanban JS={javascript}: real two-view 409, refresh/fresh version, successful retry OK.', flush=True)
                    assert not errors and not failures, (errors, failures)
                    browser.close()
                    print('No uncaught JavaScript errors or HTTP 5xx. Fictional database/server removed.', flush=True)
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == '__main__':
    main()
