# Дипломная CRM

Итерация 0: минимальный проект Django 5.2 LTS, пакет `config`, пустое приложение `crm`, SQLite и один smoke test. CRM-модули пока не реализованы.

## Локальный запуск

Нужен Python 3.12 (проверено на 3.12.14). Команды выполняются из корня репозитория:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export DJANGO_SECRET_KEY="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')"
python manage.py migrate --noinput
python manage.py runserver 127.0.0.1:8000
```

Откройте `/admin/login/` на локальном сервере. При необходимости создайте локального администратора командой `python manage.py createsuperuser`. Учётные записи не поставляются. Корень `/` не содержит бизнес-страницы. В новом терминале снова активируйте окружение и задайте `DJANGO_SECRET_KEY`; для сохранения сессий используйте один и тот же ключ через безопасное локальное окружение. Не добавляйте его в Git.

Настройки предназначены только для разработки: `DEBUG=True`, разрешены локальные хосты, SQLite, язык интерфейса русский, хранение времени UTC. Для production требуется отдельная конфигурация.

## Проверки

В том же окружении с заданным ключом:

```bash
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
python -m pip check
```

Проверено 26.10.2026: все команды завершились с кодом 0; выполнен 1 smoke test формы входа администратора. Миграции применены. Реальный HTTP-запрос к `/admin/login/` вернул 200 и форму с CSRF, `/admin/` перенаправил на вход. Визуальная проверка браузером и вход пользователя не выполнялись.

## Документация

- `AGENTS.md` — правила разработки.
- `docs/TECHNICAL_SPEC.md` — требования.
- `docs/DEVELOPMENT_PLAN.md` — этапы и критерии.
- `docs/PROGRESS.md` — фактические результаты.
- `FIRST_ITERATION_PROMPT.md` — задание итерации 0.

Использован стандартный каркас `django-admin` и зависимость Django (BSD-3-Clause): https://www.djangoproject.com/ и https://docs.djangoproject.com/en/5.2/. Код сторонних CRM не использовался. Bootstrap и бизнес-интерфейс пока не нужны и не подключены. Следующий шаг — итерация 1 (авторизация и роли), только после согласования.
