# 9Б — окончательная комплексная приёмка CRM

## 1. Контрольный коммит и среда

05.11.2026, Europe/Moscow. Проверен **`537d29842a79e72fb8b011d02996d5e082c844b6`**, исходное дерево чистое,165 отслеживаемых файлов. Python3.12.14, Django5.2.18, SQLite3.53.1, Node24.19.0, Chromium154.0.8037.57; asgiref3.13.0/sqlparse0.6.0. Это новые запуски9Б, исторические результаты не подставлялись.

Отдельный чистый clone контрольного коммита получен из локального Git (`git clone --no-local`), создан новый venv, зависимости установлены из requirements.txt. Check/457 тестов/миграции/createsuperuser/запуск/вход и девять HTTP-страниц прошли в нём. Скачивание из GitHub через браузер отдельно не проверялось. Все функциональные/браузерные испытания использовали временные синтетические SQLite, рабочие записи не копировались.

## 2. Область приёмки

Текущие CRM0–8Б, М1–М3Б, исправления9А.2, дизайнД2.5.1; локальный показ на полностью мигрированной БД0007. Изучены AGENTS, README, исходное ТЗ, план/журнал, архитектурные решения, технический аудит, математическая модель/эксперимент, протокол подтверждений и дизайн, текущие views/forms/models/services/тесты/шаблоны/скрипты.

**PASS** — актуальная проверка; **FAIL** — воспроизведённый дефект; **LIMITATION** — известная граница контракта; **NOT_TESTED** — не проверено; **NOT_APPLICABLE** — не относится к выбранной реализации. В матрице «HTTP» означает настоящий runserver и сетевой клиент, «suite» — существующие Django/unittest, «S» — чтение исходников. S не подменяет динамическое испытание.

Новые функции, бизнес-правки, тестовые модули, редизайн, изменение схемы/рабочей базы, диплом/Visio/презентация/production в объём не входят.

## 3. Матрица требований и фактических результатов

| Требование | Статус | Актуальное доказательство / граница |
|---|---|---|
|Вход, неверный пароль, inactive, защищённая главная|PASS|HTTP, suite; отказ не создаёт авторизованную сессию |
|POST-выход/CSRF, GET405, безопасный next|PASS|HTTP с отсутствующим/неверным/верным CSRF, сессия после выхода недоступна, внешний next отклонён |
|Admin/manager, ошибочный staff, оптимизация|PASS|HTTP: superuser допущен; staff-manager не входит в Admin, optimization403; чужие scoped URL404 |
|Клиенты CRUD/search/company/filter/pagination/связи|PASS|HTTP,40 браузерных CRUD-циклов на пяти типах, общая база клиентов; связи ограничены ролью |
|Удаление/PROTECT, GET подтверждения без записи|PASS|HTTP/формы: manager403, admin redirect либо200 с объяснением защиты, данные сохранены |
|Новая заявка/одна автозадача/назначения|PASS|HTTP: forced new/self, активные назначения admin, совпадают owner/UTC-срок; подмена полей не работает |
|Автоматизация атомарна, без дублей|PASS|Реальный отказ после INSERT задачи откатывает пару; повтор ensure возвращает прежний ID; редактирование не создаёт ещё задачу |
|Ручные задачи, статусы/сроки/просрочки/completed_at|PASS|HTTP: установка/сохранение/очистка completed_at; completed/cancelled не просрочены; завершение заявки и задачи независимо |
|Изоляция задач по двум ответственным|PASS|HTTP: нужны одновременно назначение задачи и её заявки; подмена request/assignee отклоняется |
|Сделки CRUD/Decimal/границы|PASS|HTTP:0.01 и9999999999.99 принимаются;0/negative/3decimal/overflow/NaN/Infinity/text отклоняются без записи |
|Пять этапов/счётчики/лимит карточек|PASS|HTTP+Chromium:20 карточек при26 записях; счётчики учитывают все, scoped по роли |
|HTML/DnD/no-JS/expected_updated_at/409/recovery|PASS|HTTP и Chromium с JS/без: два представления, настоящий stale409, refresh+retry; сетевой отказ/неопределённость/busy/error |
|История:4 типа/автор/клиент/даты|PASS|HTTP+Chromium: серверный автор, future error, подмена метаданных, права редактирования/удаления |
|История:поиск/комбинированные фильтры/пагинация/top10|PASS|Свежий verify_iteration7, обе роли/все обязательные ширины, клиентский top10 scoped |
|Аналитика:7 исходных KPI/распределения/просрочки|PASS|Независимые HTTP-контроли: total/open/closed/won/lost/conversion/won_amount, этапы/типы/текущая нагрузка |
|Аналитика:UTC all/30d/custom, ошибки/точность/роли|PASS|verify_iteration8b и suite: границы дат, Decimal, повреждённые деньги200/итог недоступен; нет ложного0 |
|Таблицы и диаграммы согласованы; сумма не названа выручкой|PASS|HTTP чисел/процентов и Chromium; количественные полосы, точные таблицы, явное пояснение |
|Модель/greedy/B&B/oracle/LB/Fraction/лимиты/ties|PASS|Новый М1,499 входов М2,66 существующих unit, независимый пересчёт; раздел7 |
|Подписи/предпросмотр/consent/транзакции/replay|PASS|Реальный HTTP+Chromium, конкурентные POST, rollback, after-COMMIT/непрочитанный ответ; раздел8 |
|CSRF/endpoint roles/GET/redirect/escaping/whitelist|PASS|S всех семейств routes + HTTP негативы, escape script в клиенте/истории/оптимизации, suite |
|Известные форматы секретов в reachable Git|PASS|686 объектов/404 текстовых blob,0 совпадений credential-patterns; tracked.env/SQLite/private-key файлов нет |
|Отсутствие любых возможных секретов/CVE|NOT_TESTED|Pattern scan не является полным security/supply-chain аудитом |
|Пустая0001→0007, синтетическая0005→0007|PASS|Миграции/check/integrity/FK/сохранность строк/старой схемы, раздел9 |
|Индексы/уникальность/backup/restore|PASS|Синтетические реальные INSERT-нарушения и SQLite backup API, включая committed WAL |
|Рабочая SQLite готова ко всем страницам0007|LIMITATION|Она на0005; migrate --check **1**,0006/0007 не применялись. Обновление требует отдельного согласия |
|Все прежние457 тестов и файлы кода сохранены|PASS|Чистый clone:457 OK; исходные test-модули/приложение побайтово неизменны |
|UI1440/768/375/320, клавиатура/local scroll/длинные данные|PASS|298 общих отображений,214 форм, целые цепочки с JS/без; раздел6 |
|Демо admin+manager, установка/запуск/остановка|PASS|Новый venv+HTTP,8 admin и8 manager сочетаний ширина/JS; практический сценарий |
|FR-06: задача также связана со сделкой|LIMITATION|Реализована только связь с заявкой; исходное ТЗ не переписано |
|FR-08 буквально: число клиентов/задачи по статусам|LIMITATION|Отдельного KPI клиентов и распределения задач по статусам нет; request/task totals есть в текущей нагрузке |
|Bootstrap из исходного ТЗ|LIMITATION|Собственный локальный CSS по прежнему решению; требования адаптивности проверены |
|DEBUG/локальный runserver — production-ready|LIMITATION|Пять dev deploy-warning; отдельный secure settings-check не заменяет deployment |
|Физический touch/другие браузеры/screen reader/полный WCAG|NOT_TESTED|Chromium headless/клавиатура не доказывают их поддержку |
|Нагрузка/отказ диска/PostgreSQL/production|NOT_TESTED|Две конкурентные операции SQLite не являются нагрузочным испытанием |
|Проверки React/Redis/Celery/внешних CRM API|NOT_APPLICABLE|Эти компоненты отсутствуют в архитектуре |

## 4. Автоматические проверки

В новом venv чистого clone выполнены **точные команды** ниже. Время тестов — runner, прочие длительности — wall процесса. Код0 не скрывает предупреждения.

| Команда | Фактический результат | Код |
|---|---|---:|
|`python manage.py check`|No issues;0.998 с|0|
|`python manage.py test`|**457 OK,26.789 с**, процесс28.440 с|0|
|`python manage.py makemigrations --check --dry-run`|No changes detected;0.710 с|0|
|`python -m pip check`|No broken requirements found;0.515 с; предупреждение недоступного pip-cache, не конфликт зависимостей|0|
|`node --check crm/static/crm/kanban.js`|Синтаксис корректен;0.060 с|0|
|`git diff --check` и staged check|Без ошибок пробелов, включая новые документы|0|
|`python manage.py migrate --noinput`, затем `migrate --check` в новом clone|Все миграции, в том числе0007, применены|0/0|
|`python manage.py createsuperuser --noinput`|Новый synthetic admin, пароль только через env|0|
|`python -m unittest crm.test_optimization`|66 OK,0.120 с, входят также в457|0|

Дополнительно22 существующих теста аналитики под `sys.unraisablehook` и `gc.collect()` после teardown: **22 OK,1.254 с,0 unraisable**. Историческая ошибка SQLite-курсора не замалчивается: исправлен тестовый lifecycle9А.2, внешний handler с долгим хранением `LogRecord.exc_info` остаётся ограничением. Новых тестов/исправлений приложения9Б нет.

Первый запуск внешнего runner hook не нашёл config из-за PYTHONPATH; после указания clone он прошёл. Первые диагностические HTTP/GUI-обёртки имели две ошибки измерителя: поле `F` вместо `objective` и ожидание302 при автоматическом следовании redirect→login200. Исправлены только временные обёртки, окончательные полные серии повторены. Эти неуспешные попытки не выдаются за дефекты CRM или успешные проверки.

## 5. HTTP-проверки

Новая auth/CRUD-серия: **120 настоящих запросов,11 групп PASS,18.424 с,exit0**. Проверены неверный/inactive вход; отсутствующий/неверный/верный CSRF; logout GET405 и POST; безопасный next; superuser и manager со staff; изоляция detail/edit/list/client relations; CRUD/search/filter/pagination/PROTECT; lifecycle заявок/ручных и автоматических задач; границы денег. Административный `/admin/` менеджеру перенаправляется на admin-login, который отвергает его учётные данные; это не ошибочное утверждение, что любой admin-GET обязан возвращать403.

Существующие регрессии повторно проверили историю/аналитику/Kanban. JSON-ошибки Kanban400/401/403/404/405/409/500 и HTML409 получены реальными запросами; контролируемые500/503 вводились только диагностическим сервером. При ошибке/conflict нет запрещённой записи или утечки внутренних деталей. Обычные сценарии не дали неожиданных5xx.

Для повторения постоянных HTTP/browser-проверок из корня, где `python` имеет Playwright, а `APP_PYTHON` — Python venv приложения:

```bash
export APP_PYTHON=/absolute/path/to/app-venv/bin/python
python scripts/verify_iteration7.py --python "$APP_PYTHON" --chromium /usr/bin/chromium --port 8092 --artifacts /tmp/crm9b-history
python scripts/verify_iteration8b.py --python "$APP_PYTHON" --chromium /usr/bin/chromium --port 8093 --artifacts /tmp/crm9b-analytics
python scripts/verify_stage6c.py --python "$APP_PYTHON" --chromium /usr/bin/chromium --port 8094 --artifacts /tmp/crm9b-kanban
```

Эти файлы сами создают временные настройки/синтетическую БД и удаляют её; app requirements не включают Playwright. В9Б прежние сценарии исполнялись внешней обёрткой с единственным добавлением320 к циклам ширин, без изменения исходников/assertions. Чтобы точно повторить расширение, любую команду выше можно вызвать как `python - <script> <те же аргументы> <<'PY'` с таким телом:

```python
from pathlib import Path
import sys
original = Path(sys.argv[1]); sys.argv = sys.argv[1:]
source = original.read_text(); changed = 0
for old, new in [
    ('(1440,1280,768,375)', '(1440,1280,768,375,320)'),
    ('(1440, 1280, 768, 375)', '(1440, 1280, 768, 375, 320)'),
    ('[1440,1280,768,375]', '[1440,1280,768,375,320]'),
]:
    changed += source.count(old); source = source.replace(old, new)
assert changed
exec(compile(source, str(original), 'exec'),
     {'__name__': '__main__', '__file__': str(original)})
```

Для ручного повторения auth/CRUD — [демосреда](DEFENSE_DEMO.md), два менеджера/один superuser и служебный inactive/staff-manager; ожидаемые статусы и поля перечислены в матрице. Постоянные автоматические эквиваленты: `python manage.py test crm.tests crm.test_clients crm.test_requests crm.test_tasks crm.test_automation crm.test_deals`. Временные диагностические программы/логи9Б находятся вне Git; их пути не используются как единственный способ повторения.

## 6. Браузерные проверки

| Новая серия | Результат | Wall, с / код |
|---|---|---|
|verify_iteration7|История/права/даты/текст/пагинация/409,1440/1280/768/375/320|31.527 /0|
|verify_iteration8b|Независимые деньги/UTC/проценты, повреждения, диаграммы, те же ширины|32.194 /0|
|verify_stage6c|Формы/keyboard/DnD/network/409/recovery, с JS/без, те же ширины|103.983 /0|
|Переиспользованный сценарий форм Д2.5|214 отображений;40 CRUD/error/edit/cancel циклов=5 типов×2 роли×1440/1280/768/375; отдельно5 create-reflow форм320|141.346 /0|
|Переиспользованный общий UI-сценарий|298 отображений: home/sidebar/пять CRUD/ошибки/detail/Kanban/analytics/optimization, обе роли,1440/768/375/320, настоящий zoom200/400%|133.679 /0|
|Демо и подтверждение|8 admin+8 manager сочетаний=4 ширины×JS on/off, нормальное создание и after-COMMIT/retry|75.878 /0|

Единая admin-цепочка: вход → главная → клиенты → заявки → задачи → сделки → Kanban → история → аналитика → М1 → сравнение → signed preview → confirm → receipt. Использованы настоящие ссылки/Enter,72 навигационных перехода в8 admin-прогонах. Manager прошёл ограниченную цепочку без административной оптимизации. Элементы не перекрываются, критические данные/длинный текст не скрыты, таблицы/доска имеют локальный scroll и клавиатурный доступ, предупреждения видны. Это конечный набор проверок, не доказательство всех возможных текстов.

Регрессии Д2.5.1 не обнаружены: первые пять контрольных карточек1440/768 —544 px;375/320 —535.391/486.781/580.172/486.781/535.391 px. На1440 обе нижние панели главной474.391 px. Forms214 не объявляются40 полными циклами на320;320 дополнительно проверен UI-серией и формами reflow.

### Новые постоянные доказательства

Только **10 новых PNG,1 298 935 байт**; старые материалы Д2.5/Д2.5.1 не изменялись/не копировались. Вымышленные audit/ui/history/analytics-аккаунты/клиенты; ключи/пароли случайные и не отрисовывались. Проверены полное декодирование/отсутствие PNG-метаданных/происхождение/видимое содержимое/ссылки. ZIP/БД/traces/logs/backup в evidence нет.

| Материал | Назначение |
|---|---|
|[Главная1440](evidence/9b/home-1440.png)|Текущие счётчики на синтетике, панели Д2.5.1 |
|[Kanban320: ошибка сети](evidence/9b/kanban-network-320.png)|Заметное предупреждение неопределённости, refresh и локальная доска |
|[Форма заявки320](evidence/9b/request-form-320.png)|Нативные поля/подписи/UTC/действия без JS |
|[Взаимодействие320](evidence/9b/interaction-detail-320.png)|Длинные данные, авторство/дата, script показан текстом |
|[Аналитика320](evidence/9b/analytics-320.png)|Семь KPI, UTC, диаграммы/точные таблицы/просрочки |
|[Оптимизация1440](evidence/9b/optimization-1440.png)|Реально вычисленные greedy/B&B и границы трактовки |
|[Предпросмотр375](evidence/9b/preview-375.png)|Подписанный план/реквизиты/согласие без JS |
|[Созданный пакет320](evidence/9b/created-320.png)|Две заявки/две автозадачи, квитанция |
|[After-COMMIT320](evidence/9b/after-commit-320.png)|Настоящий503: пакет мог сохраниться, прежний токен/согласие/повтор |
|[Менеджер: аналитика375](evidence/9b/manager-analytics-375.png)|Scoped показатели, нет административной оптимизации |

## 7. Математическая верификация

Модель соответствует коду: множество работ/выбранных сотрудников, квалификация допустимых пар, экспертные минуты, ставки, A/L/H=A−L, C=t·r, U=max((L+p)/A), F=αC/C₀+βU, α+β=1. C₀ — сумма максимальных стоимостей квалифицированных пар по строкам; только при нулевой сумме заменяется1. Часы/стоимости/метрики — точные Fraction, float не участвует в выборе.

Greedy — порядок заявок по ID, минимум допустимого ΔF/userID; B&B — полный greedy incumbent, capacity/qualification, LB независимых минимумов стоимости+текущий U, строгое LB>best, лексикографические равенства; oracle перебирает полные векторы независимо от greedy/LB.66 unit включают120 независимо вычисленных малых задач, LB на30 наборах, границы лимитов, статусы/детерминизм/неизменность входа. OPTIMAL/INFEASIBLE отличаются от FEASIBLE_NOT_PROVEN/UNKNOWN; тупик greedy не доказывает невозможность. Худший поиск экспоненциальный, frontier gap отсутствует, таймер кооперативный.

М1 **вычислен core, oracle и HTTP/UI**, ввод — [таблица демо](DEFENSE_DEMO.md#числа-для-примера-м1):

| Метод | Назначения | C | U | T, ч | F |
|---|---|---:|---:|---:|---:|
|Greedy|1→1,2→2|1000|1|13/4|23/26|
|B&B/oracle|1→2,2→1|600|1|3|19/26|

C₀=1300, относительное улучшение F=4/23≈17.3913%; B&B5 узлов/oracle4 вектора.40% разницы условных затрат **не являются реальной экономией предприятия**.

М2 повторён с исходными seeds20261028–20261047, rho=3/4, K2/4:19 controls плюс480 random=499 уникальных входов. Controls повторены5 раз; small/medium/large повторены отдельно; smoke27 и8 уже учтённых large с высоким бюджетом — дополнительные запуски, не новые уникальные кейсы. В основной серии нет time cutoff/unrun.

| Серия | OPTIMAL | INFEASIBLE | FEASIBLE_NOT_PROVEN | UNKNOWN | F строго лучше / сравнимые пары |
|---|---:|---:|---:|---:|---:|
|controls19|17|2|0|0|3/14|
|small160|131|29|0|0|22/129|
|medium160|53|32|69|6|93/116|
|large160|0|29|109|22|81/106|
|Итого499|201|92|178|28|199/365|

14 тупиков greedy получили полный B&B-план; oracle179 уникальных controls/small без расхождений. Независимый пересчёт JSON без импорта solver подтвердил1942 полных результата,484 контрольных назначения и339 oracle-сравнений **с учётом повторов**. Comparison-signatures совпали с историческими и новым повтором: small `573cddd4ff18c8c6dc271001cf38edabcc6148287f664aa08a6f8cfcc11096d0`, medium `e8e7523a9edad8fa44681b5c1050e27729947121a7078b28fa25c5b68d0dd489`, large `6531c5a2cc458b8b5b400a36d228d9361f6e25c3df04283b9c5290749d8358f3`.

Точные постоянные команды из корня (выходы — только вне Git); все реально выполнены, exit0:

```bash
python -m unittest crm.test_optimization
python scripts/run_optimization_experiment.py --preset smoke --max-cases 27 --output /tmp/crm9b-smoke.json
python scripts/run_optimization_experiment.py --preset controls --max-cases 19 --repeats 5 --output /tmp/crm9b-controls.json
python scripts/run_optimization_experiment.py --preset small --seed-count 20 --max-cases 160 --output /tmp/crm9b-small.json
python scripts/run_optimization_experiment.py --preset medium --seed-count 20 --max-cases 160 --node-limit 2000 --time-limit 3 --output /tmp/crm9b-medium.json
python scripts/run_optimization_experiment.py --preset large --seed-count 20 --max-cases 160 --node-limit 500 --time-limit 3 --output /tmp/crm9b-large.json
python scripts/run_optimization_experiment.py --preset large --seed-count 1 --max-cases 8 --node-limit 200000 --time-limit 30 --output /tmp/crm9b-highbudget.json
```

Wall основного запуска: controls0.139/small0.482/medium3.568/large3.429 с; highbudget35.747 с,7FEASIBLE_NOT_PROVEN/1UNKNOWN, оптимальность не заявляется. Методика/генератор остаются в [М2](OPTIMIZATION_EXPERIMENTS.md), математические границы — в [М1](OPTIMIZATION_MODEL.md).

## 8. Подписи и транзакции

Отдельный новый HTTP-прогон24.442 с,exit0, плюс браузерные цепочки раздела6. Временный сервер с диагностическими patch использовал действующие views/services и SQLite0007; fault injection не входит в опубликованное приложение. Подпись не является шифрованием.

| Сценарий | Фактический результат |
|---|---|
|Ввод→расчёт→prepare→preview→confirm→result→replay|М1C600/U1/T3/F19/26, ровно2 заявки/2 задачи/1 журнал; повтор — те же ID |
|Подпись/математические поля/подмена доказанности|400, записей нет; реальный FEASIBLE_NOT_PROVEN не повышается до OPTIMAL |
|Чужой admin/manager/staff; CSRF; GET|Чужой admin403/result404; manager/staff403; отсутствие CSRF403; GET prepare/preview/confirm405 |
|Иной корректно подписанный digest того же UUID|409, прежний пакет не меняется |
|Недоказанный полный план|Без дополнительного согласия400, с согласием допускается; не выдаётся за доказанный |
|Два настоящих одновременных POST|302/302, один журнал и одна пара заявок/задач на строку плана |
|Поздняя ошибка после записи последней строки|503, полный rollback journal/requests/tasks; тот же token после снятия fault успешно создаёт пакет |
|Ошибка формирования ответа после COMMIT|503, сообщение «Пакет мог быть создан», тот же hidden snapshot/согласия/повтор; прежние ID без роста |
|Ответ не прочитан клиентом, затем повтор|Соединение закрыто без чтения ответа; baseline1/1/0→3/3/1, replay остаётся3/3/1 |
|Ротация ключа|Без прежнего ключа токен400; с явно заданным SECRET_KEY_FALLBACKS во временной конфигурации — прежняя квитанция без новой записи; постоянные settings fallback не настраивают |
|Обычная заявка: отказ после task INSERT|Форма200 с ошибкой, обе записи откатились; повтор+1/+1; ensure дважды возвращает тот же ID |

Повторяемая оператором положительная цепочка и восстановление — [DEFENSE_DEMO](DEFENSE_DEMO.md). Постоянные регрессии fault/signature/concurrency уже в коде, новых тестов нет:

```bash
python manage.py test crm.test_optimization_commit crm.test_automation crm.test_stage_conflicts
```

Точные существующие контрольные методы: `OptimizationCommitTests.test_late_request_failure_rolls_back_every_request_task_and_receipt`, `test_unproven_status_upgrade_with_original_signature_is_rejected`; `OptimizationConcurrencyTests.test_response_failure_after_commit_replays_complete_package_without_duplicates`, `test_two_real_connections_start_confirmation_concurrently` в [test_optimization_commit.py](../crm/test_optimization_commit.py). Они выполнены в457. HTTP-диагностика дополняет, а не переименовывает эти TestCase в HTTP.

Протокол воспроизведения fault-серии: поднять отдельный runserver на временной0007 с fixtures М1/двумя admin/manager; пройти POST с cookie+CSRF; сохранить точный snapshot. Только в этом диагностическом процессе обернуть `optimization_commit.create_request_with_task` ошибкой **после** вызова оригинала на последней строке и сравнить три таблицы до/после503. Для after-COMMIT обернуть формирование redirect в `views` ошибкой после возврата commit, проверить уже сохранённый пакет и повторить исходный POST после отключения fault. Два отдельных клиента/соединения синхронизировать barrier перед POST одного snapshot. Подмена HMAC выполняется без повторной подписи; иной digest — серверно подписанная диагностическая альтернатива того же UUID. В релизе нет пользовательских fault-header/параметров или обхода подписи.

<a id="database-operations"></a>
## 9. База, миграции и резервирование

Рабочая база прочитана через URI `mode=ro`: crm0001–0005, integrity `ok`, foreign_key_check пусто. **Фактический `migrate --check` вернул1**, миграции не применялись. SHA-256 до/после: `78b956ec4d9693ac10bb1591114ed9a1fb5316e5524552138d92eae964a07aac`. Рабочие данные/backup не помещались в Git.

Пустая временная БД прошла0001–0007/check0. Отдельная заполненная синтетическая0005→0007 сохранила все значения User1/Client1/Request1/Task2/Deal1 и старые CRM-таблицы/индексы; новые history/commit таблицы пустые. Существуют `interaction_author_time`, `interaction_client_time`, partial unique `one_automatic_task_per_request`, PK UUID журнала и FK; реальные повторные INSERT автозадачи/токена и amount0 отвергнуты IntegrityError. Проверена последовательная зависимость всех семи исходных миграций, их файлы не менялись.

Синтетический SQLite backup API и восстановление подтвердили значения/историю/integrity/FK; отдельно подтверждено чтение committed-строки из действующего WAL. Аппаратная потеря питания/реальное восстановление рабочей базы не проверялись.

### Безопасный backup и обновление своей базы

**Следующие действия — инструкция для отдельного согласованного обновления; на рабочей облачной БД9Б они не выполнялись.**

1. Остановить все процессы приложения и другие writers этой SQLite. Для foreground runserver — Ctrl+C в его терминале. Убедиться, что соединений нет; не применять массовый pkill. Сохранить исходный commit и SHA-256 файла приватно. Оставить прежний SECRET_KEY.
2. Указать новый, ещё не существующий путь backup вне Git/evidence/публичного каталога. Backup содержит данные/парольные хеши: права0600 и закрытое хранилище. На остановленной базе выполнить API ниже, не сырой cp одного файла при возможном WAL:

```python
from contextlib import closing
from pathlib import Path
import os, sqlite3
source = Path('db.sqlite3').resolve()
backup = Path('/private/path/outside/repository/crm-before-0007.sqlite3')
assert source.is_file() and not backup.exists()
with closing(sqlite3.connect(source)) as live:
    checkpoint = live.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    assert checkpoint[0] == 0, checkpoint
    backup.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(backup)) as target:
        live.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert target.execute('PRAGMA foreign_key_check').fetchall() == []
        print(target.execute("SELECT name FROM django_migrations WHERE app='crm' ORDER BY name").fetchall())
os.chmod(backup, 0o600)
```

`(0,-1,-1)` checkpoint в DELETE journal mode нормален; busy должен быть0. При ошибке backup не продолжать migration. После проверки backup, в активном venv с прежним ключом и всё ещё остановленными writers:

```bash
python manage.py showmigrations crm
python manage.py migrate --plan
python manage.py migrate --noinput
python manage.py migrate --check
python manage.py check
```

3. Повторить integrity/FK/контроль прежних записей; проверить `[X]`0006/0007. Запустить локально, войти, открыть историю/оптимизацию; лишь затем считать обновление законченным. Текущая рабочая0005 до этого не пригодна для полной демоцепочки.
4. При отказе оставить приложение остановленным. Сохранить неудавшуюся БД API в другой приватный файл. Из проверенного backup подготовить **новый** восстановленный файл через backup API, проверить integrity/FK/migrations. При отсутствии всех соединений переместить прежние main/`-wal`/`-shm` вместе в приватный диагностический каталог; не смешивать старые sidecars с восстановленным main. Поставить восстановленный файл на штатный путь, проверить владельца/права0600. Это инструкция, не разрешение удаления рабочей базы этой задачей.
5. Восстановленная0005 несовместима с нынешними страницами0007: сначала повторить исправленное обновление либо выбрать заранее проверенный совместимый код. Не использовать слепой `migrate crm 0005`: это может удалить новые данные. Для сброса **демо** использовать checkpoint0007 и тот же ключ.

### Повтор миграционной проверки без рабочей БД

В новой папке установка/`migrate` из README повторяет пустой прогон. Upgrade воспроизводится существующими migration-тестами [истории](../crm/test_interactions.py) и [подтверждений](../crm/test_optimization_commit.py); оба вошли в457. Для отдельного полного0005→0007 drill ниже — самостоятельная команда из корня с заданным DJANGO_SECRET_KEY; она подменяет только DATABASES в памяти и создаёт/удаляет временную БД:

```bash
python - <<'PY'
import importlib, os, sys, tempfile, types
from decimal import Decimal
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='crm9b-upgrade-') as tmp:
    base = importlib.import_module('config.settings')
    settings = types.ModuleType('acceptance_temp_settings')
    for key in dir(base):
        if key.isupper(): setattr(settings, key, getattr(base, key))
    settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                                     'NAME': str(Path(tmp) / 'synthetic.sqlite3')}}
    sys.modules[settings.__name__] = settings
    os.environ['DJANGO_SETTINGS_MODULE'] = settings.__name__
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.core.management import call_command
    target = [('crm', '0005_deal'), ('auth', '0012_alter_user_first_name_max_length')]
    executor = MigrationExecutor(connection); executor.migrate(target)
    apps = executor.loader.project_state(target).apps
    User = apps.get_model('auth', 'User')
    Client = apps.get_model('crm', 'Client')
    Request = apps.get_model('crm', 'ClientRequest')
    Task = apps.get_model('crm', 'Task'); Deal = apps.get_model('crm', 'Deal')
    user = User.objects.create(username='synthetic-migration-user')
    client = Client.objects.create(full_name='Учебная миграция')
    request = Request.objects.create(client=client, title='Учебная заявка', assigned_to=user)
    Task.objects.create(request=request, title='Авто', assigned_to=user, is_automatic=True)
    Task.objects.create(request=request, title='Ручная', assigned_to=user)
    Deal.objects.create(client=client, title='Учебная сделка', assigned_to=user, amount=Decimal('12.34'))
    tables = ('auth_user', 'crm_client', 'crm_clientrequest', 'crm_task', 'crm_deal')
    def snapshot():
        with connection.cursor() as cur:
            return {table: cur.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()
                    for table in tables}
    before = snapshot()
    call_command('migrate', interactive=False, verbosity=0)
    call_command('migrate', check=True, verbosity=0)
    assert snapshot() == before
    with connection.cursor() as cur:
        assert cur.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert cur.execute('PRAGMA foreign_key_check').fetchall() == []
    connection.close()
    print('SYNTHETIC_0005_TO_0007_OK')
PY
```

## 10. Ограничения и остаточные риски

Новых подтверждённых P0/P1 в проверенном объёме нет; это не утверждение отсутствия неизвестных дефектов. Исправлять бизнес-код автоматически оснований не возникло.

| Уровень / статус | Остаточная граница | Нужно до локального показа |
|---|---|---|
|P2 / LIMITATION|Рабочая0005, нет0006/0007|Использовать новую демо0007; обновление рабочей — отдельно с backup/согласием |
|P2 / LIMITATION|FR-06/FR-08 и CSS вместо Bootstrap отличаются от буквального ТЗ|Описывать фактический объём, не обещать отсутствующие возможности; ТЗ сохранено |
|P2 / LIMITATION|SQLite/Decimal: прямой ORM/SQL может нарушить деньги,1.234→1.23 после ORM не всегда обнаружимо|Демонстрировать ввод через формы; trusted скрипты full_clean+Decimal из строк |
|P2 / LIMITATION|A-02: внешний handler может удержать exc_info/курсор|Не выдавать тестовую очистку за исправление любого logging; [исходный лог](validation/iteration8b/legacy-analytics.log) сохранён |
|P2 / LIMITATION|Нет глобального резервирования/расписания/индивидуальной гарантии дедлайна|Оценки экспертные; календарную гарантию/реальную экономию не заявлять |
|P2 / LIMITATION|Нет общего optimistic lock CRUD/снимка нескольких SELECT|При409/неопределённой сети штатное обновление/повтор; не обещать общий контроль гонок |
|P2 / LIMITATION|Нет rate limiting/password reset, DEBUGTrue/локальные hosts, SQLite/runserver|Только локальная закрытая демосреда, без production-публикации |
|P3 / LIMITATION|Native select/date, переносы/локальная прокрутка, разное число quick-links|Не блокирует проверенный показ; повторный редизайн не нужен |
|NOT_TESTED|Physical touch/другие браузеры/полный WCAG/нагрузка/авария диска|Проверить на собственном устройстве перед защитой; не считать автоматически пройденным |

Dev `check --deploy` реально дал0 **с пятью предупреждениями** security.W004/W008/W012/W016/W018. На отдельном временном settings с DEBUGFalse/HTTPS redirect/secure cookies/HSTS и случайным ключом —0 без предупреждений. Постоянные настройки не менялись; TLS/сервер/proxy/deployment этим не испытаны. SECRET_KEY обязателен из env, ALLOWED_HOSTS локальные; отсутствие любого возможного секрета не доказано одним pattern scan.

## 11. Демонстрационный сценарий

Отдельный компактный [DEFENSE_DEMO](DEFENSE_DEMO.md) содержит предварительное создание синтетического набора, шаги/время/ожидаемые результаты, точный ввод М1, безопасный повтор и сброс демосреды. Набор включает admin/двух менеджеров/клиентов/заявки с автозадачами/ручные задачи/все этапы/все типы/просрочки/аналитику/расчёт/подтверждение. Временные базы с хешами паролей не являются поставляемыми evidence; набор воспроизводится по инструкции.

## 12. Итоговое решение

**PASS: техническая приёмка9Б выполнена для локальной демонстрации на отдельной полностью мигрированной синтетической SQLite0007 с перечисленными ограничениями.** Новых незакрытых P0/P1 не обнаружено,457 тестов сохранены, основные HTTP/GUI-цепочки/математика/конкуренция/транзакции/миграции воспроизведены. Это внутренний проверяемый акт, окончательная оценка защиты и принятие ограничений остаются за человеком.

**Рабочая облачная SQLite0005 не обновлена и пока не готова для всей демоцепочки.** Человеку нужно подготовить отдельную демо0007 по инструкции, приватно сохранить ключ/backup, пройти показ на своём устройстве; обновлять пользовательскую БД лишь отдельно согласованной процедурой. Production, написание диплома, Visio и презентация не начаты.

Изменения9Б — только README/PROGRESS/DEVELOPMENT_PLAN, этот акт/DEFENSE_DEMO и10 новых PNG:165→177 файлов,162 прежних файла побайтово неизменны. Исходники, CSS/JS,457 тестов, миграции, старые доказательства/архитектурные решения/ТЗ сохранены. Проверены303 локальные ссылки и82 Markdown-фрагмента; опубликованные здесь команды временного upgrade и backup отдельно исполнены на синтетике. Полные временные логи/JSON/диагностические скрипты/базы остаются вне Git, ZIP-дубликатов нет. Материалы в репозитории доступны вместе с кодом через GitHub Code → Download ZIP. После публикации отдельного коммита9Б работа останавливается.
