# Проверка стабилизации 6C — 26.10.2026

База: `ba4badf75342749535b1fcb50c4384eebf559525`, чистое дерево перед работой. До изменений сохранён тег `iteration-6b2`. Python 3.12.14, Django 5.2.18. Исходный полный набор — **193/193 OK, exit 0**; итог — **205/205 OK, exit 0**. Добавлены 12 методов `crm.test_stage_conflicts`, прежние методы/проверки не удалены. Это стабилизация 6C, итерация 7 не начата.

## Атомарность и границы

HTML-форма и DnD передают обязательный `expected_updated_at` (ISO UTC с микросекундами) вместе с `stage`. DateTimeField/ChoiceField проверяют формат; timestamp не является правом доступа. Для настоящего перехода ORM выполняет:

```python
Deal.objects.visible_to(request.user).filter(
    pk=deal.pk, stage=previous, updated_at=expected,
).update(stage=stage, updated_at=max(timezone.now(), expected + timedelta(microseconds=1)))
```

SQL для менеджера: `UPDATE crm_deal SET stage=?, updated_at=? WHERE id=? AND assigned_to_id=? AND stage=? AND updated_at=?`. Сравнение версии и полномочий является частью того же UPDATE. Предварительное Python-сравнение — только быстрый отказ. SQLite select_for_update не используется. Ноль обновлённых строк означает повторную проверку доступности: 404 потерянного/удалённого объекта, иначе 409. HTML 409 показывает общую ошибку/фиксированную ссылку; JSON 409 не содержит чужих данных. JS оставляет старый DOM, показывает обновление, после перечитывания получает новые версии. No-op с актуальной версией проверяет EXISTS и ничего не записывает; повтор уже выполненного POST со старой версией — 409.

updated_at выбран вместо expected_stage ради ABA (A→B→A). При совпадении/откате часов версия всё равно возрастает. Это защита endpoint смены этапа, не глобальная версия всех моделей: обычный CRUD редактирования не получил optimistic locking; доверенные прямые update/SQL должны обновлять updated_at. Другие вкладки могут менять карточки после ответа, счётчики и списки не являются общей транзакционной фотографией. Нагрузочные гонки/все возможные конкурентные записи не объявляются проверенными.

## Финансовая точность

Нормальные формы возвращают Decimal и отклоняют переполнение/больше двух знаков; DB CheckConstraint защищает amount>0. Новый тест на изолированной SQLite показывает: прямой ORM create с Decimal('10000000000.00') проходит, SELECT видит 10000000000, а ORM refresh вызывает InvalidOperation из-за Decimal(12,2). max_digits/scale/точность прямой ORM/SQL-записи SQLite не гарантирует. Это документированная граница доверия, не исправление финансового backend.

Правило дальнейшей работы: пользовательский ввод через ModelForm; доверенные импорты через Decimal из строк и full_clean перед save. Будущей аналитике сначала валидировать исходные деньги и согласовать арифметику Decimal/округление, не обещать точный SUM SQLite. Новая валюта/СУБД/аналитика не внедрялись.

## Каждое изменение прежних тестов

Точный diff относительно тега: [previous-tests.diff](validation/stage6c/previous-tests.diff). Новые helper `version()` читают актуальный timestamp только для адаптации прежних последовательных сценариев; намеренно устаревшие версии проверяются явно новым модулем. Число прежних методов остаётся 193, все assertions сохранены.

`crm/test_kanban.py`:

| Метод | Изменение и причина |
|---|---|
| test_admin_and_manager_can_move_across_all_stages | Добавлен текущий timestamp каждой целевой сделки, чтобы переходы соответствовали обязательному контракту. |
| test_new_won_contact_updates_counts_from_database | Свежая версия перед каждым последовательным POST; проверки БД/счётчиков прежние. |
| test_only_stage_and_updated_at_can_change_forged_fields_ignored | Добавлена ожидаемая версия; подмена остальных полей и сравнение всех полей сохранены. |
| test_same_stage_is_noop_and_no_duplicate | Актуальная версия для допустимого no-op; проверка полной неизменности сохранена. |
| test_invalid_and_missing_stage_400_without_mutation | Валидная версия отделяет ошибку stage от отсутствия новой версии; все исходные плохие stage сохранены. |
| test_csrf_missing_invalid_and_valid | Только успешный CSRF POST получает версию; отрицательные CSRF-проверки не менялись. |
| test_external_redirect_parameters_ignored | Валидная версия позволяет дойти до прежнего фиксированного redirect; внешние параметры оставлены. |
| test_reassignment_changes_cards_counts_and_mutation_access | Добавлена версия в положительный POST нового владельца; 404 прежнего владельца и counts сохранены. |

`crm/test_dragdrop.py`: общий `post()` добавляет актуальную версию с возможностью явного переопределения через data. Это адаптирует все 12 исходных сценариев без удаления проверок: `test_html_redirect_and_explicit_json_are_compatible`, `test_only_stage_updated_at_change_and_no_new_objects`, `test_noop_preserves_timestamp_and_returns_one_column`, `test_invalid_missing_stage_and_get_json_errors`, `test_foreign_missing_and_anonymous_json_errors`, `test_csrf_json_failure_and_success_and_html_failure_unchanged`, `test_fragments_and_counts_scoped_and_html_escaped`, `test_admin_all_columns_include_foreign_records`, `test_full_columns_refill_and_never_exceed_limit`, `test_sequential_moves_follow_database_and_leave_empty_columns`, `test_json_data_queries_are_bounded`, `test_server_failure_returns_generic_json_not_traceback`.

Дополнительно внутри этого модуля изменены четыре метода:

| Метод | Изменение и причина |
|---|---|
| test_html_redirect_and_explicit_json_are_compatible | Два прямых HTML POST получают обязательную версию. |
| test_csrf_json_failure_and_success_and_html_failure_unchanged | Прямой успешный JSON POST получает версию; отказы CSRF сохранены. |
| test_json_data_queries_are_bounded | Версия добавлена до RequestFactory/измерения; ожидание 5 запросов/двух LIMIT 20 не менялось. |
| test_server_failure_returns_generic_json_not_traceback | Ошибка инъецируется в DealQuerySet.update вместо Deal.save: реальная запись теперь CAS UPDATE. Assertions 500/нет приватных сведений/нет мутации прежние. |

Остальные тестовые файлы не менялись. Новый модуль проверяет две старые вкладки, свежую версию, stale repeat/актуальный no-op без UPDATE, ABA/совпадение часов, HTML/JSON 409, плохие/поддельные версии и stage, expected_stage без обхода версии, назначение между SELECT/UPDATE и при no-op, SQL WHERE, детерминированное вмешательство второго writer перед CAS, удаление, новые токены DOM и деньги. Это воспроизводимые чередования операций; многопоточная нагрузка SQLite ими не подменяется.

## Команды и результаты

В активном Python 3.12 venv с DJANGO_SECRET_KEY из безопасного окружения, в корне проекта:

| Команда | Результат | Exit |
|---|---|---|
| python manage.py check | System check identified no issues | 0 |
| python manage.py test | Ran 205 tests — OK | 0 |
| python manage.py test crm.test_deals crm.test_kanban crm.test_dragdrop | Ran 54 tests — OK | 0 |
| python manage.py makemigrations --check --dry-run | No changes detected | 0 |
| python manage.py migrate --check | Схема актуальна | 0 |
| python -m pip check | No broken requirements found | 0 |
| node --check crm/static/crm/kanban.js | Синтаксис корректен | 0 |

Полный вывод: [baseline.log](validation/stage6c/baseline.log), [checks.json](validation/stage6c/checks.json), [e2e.log](validation/stage6c/e2e.log). pip сообщил лишь об отключённом недоступном кэше, проверка зависимостей успешна.

## HTTP и Chromium

Сохранён `scripts/verify_stage6c.py` без новых runtime-зависимостей приложения. Для него отдельно нужны установленный Playwright Python и Chromium. Если они уже доступны, команда из корня:

```bash
python scripts/verify_stage6c.py --python /path/to/app-venv/bin/python \
  --chromium /path/to/chromium --artifacts /tmp/crm-stage6c
```

При необходимости используйте отдельное окружение проверки: `python3.12 -m venv /tmp/crm-e2e-venv`, `/tmp/crm-e2e-venv/bin/pip install playwright`; браузер укажите через --chromium (или установите отдельно и передайте его путь). Не добавляйте Playwright в requirements приложения ради этой проверки. --python указывает на Django venv; --port позволяет выбрать свободный локальный порт (по умолчанию 8063). Скрипт создаёт случайные секрет/пароли только в памяти, временную settings/SQLite, применяет миграции, запускает/останавливает сервер и удаляет БД в finally. Пользовательскую БД не использует.

Фактический запуск в этой среде: `python -u scripts/verify_stage6c.py --python /workspace/venvs/crm_diplom/bin/python --artifacts /workspace/validation/iteration6c`, exit 0. Артефакты там: server.log, conflict-*.png, html-conflict-*.png, js-board-*.png, network-error-*.png, board/last-column/moved-*.png. Тестовые данные вымышленные, БД удалена.

- HTTP администратора/двух менеджеров: HTML и JSON, все пять этапов, лимит/полные scoped counts, CSRF, 400/401/403/404/405/409/500, stale HTML 409, подмена суммы/ответственного/клиента/названия, fixed redirect, no-op и переназначение. Реальный 500 вызван временным SQLite-триггером только в тестовой БД: данные не изменились, приватный текст ошибки не попал в JSON. Триггер удалён, схема до/после совпала.
- Chromium 154 с JS и без JS на 1440/1280/768/375 px: все этапы, 20 карточек, длинный текст, label/ID, клавиатура, последняя колонка/горизонтальный скролл, без переполнения страницы. Без JS конфликт создаёт конкурирующий реальный HTTP writer, старая форма получает HTML 409, обновление показывает новый этап.
- С JS реальные две вкладки: вторая сохраняет, старая получает 409, старый DOM не переезжает; ссылка перечитывает свежие версии, следующий DnD успешен. Проверены последовательные переносы/счётчики/дубли, busy lock, отсутствие навигации на успехе, подставленные 400/403/404/409/500, обрыв сети, неожиданный HTML и реальный 15-секундный таймер AbortController. На 1440/1280 — mouse drag_to; на 768/375 — DragEvent/DataTransfer после прокрутки, не физический touch DnD. Нет pageerror или неожиданных 5xx; намеренный 500 проверялся отдельно. Скриншоты конфликтов и доски просмотрены.
- Исправлены только ошибки нового проверочного скрипта: попытка прочитать hidden-версию карточки, недоступной второму менеджеру, и открытие второй страницы в неявном Playwright context. Сценарий исправлен на доступный/фиктивный timestamp для отказа и явный context; успешный повтор всех режимов зафиксирован в e2e.log. Код приложения не менялся ради обхода этих ошибок.

## Целостность, файлы и ручная приёмка

Модели/сервисы/миграции/requirements не менялись. До изменений сделана backup-копия development SQLite вне Git; сравнение всех строк всех таблиц и sqlite_master после тестов идентично. [data-integrity.log](validation/stage6c/data-integrity.log) содержит digest без персональных данных. Резервная БД не включена в репозиторий/ZIP. Исходная стабильная версия доступна по iteration-6b2.

Изменены: README.md; crm/forms.py, views.py, static/crm/kanban.js, templates/crm/_kanban_card.html и deal_stage_error.html; прежние test_kanban.py и test_dragdrop.py; docs/PROGRESS.md, DEVELOPMENT_PLAN.md, ARCHITECTURE_DECISIONS.md, AUDIT_HANDOFF.md. Добавлены: crm/templates/crm/deal_stage_conflict.html, crm/test_stage_conflicts.py, scripts/verify_stage6c.py, этот отчёт и docs/validation/stage6c/{previous-tests.diff,baseline.log,checks.json,e2e.log,data-integrity.log}. Diff всей итерации: `git diff iteration-6b2 HEAD` после итогового коммита.

Человеку: две вкладки одной сделки под двумя имеющими доступ пользователями, первый перенос/второй конфликт/обновление/повтор; текущий и старый same-stage; переназначение старой карточки; телефон/прокрутка/форма/клавиатура, сеть/сессия. Проверьте границы денег через форму, не создавайте некорректные суммы в пользовательской БД. FR-06 реализован только для заявки, CSS локальный вместо Bootstrap — явные ограничения MVP без правки ТЗ. Нагрузку, полный WCAG, физический touch DnD, другой браузер/СУБД и production не проверяли. План: 7 взаимодействия, 8 аналитика, 9 финальный аудит/диплом. Ничего из этапа 7 не реализовано.
