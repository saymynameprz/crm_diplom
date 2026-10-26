from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .analytics import AnalyticsPeriod, build_report, closed_conversion, validate_amount
from .forms import AnalyticsPeriodForm
from .models import Client, ClientInteraction, ClientRequest, Deal, Task
from .views import analytics

AT = datetime(2026, 10, 26, 12, 0, tzinfo=dt_timezone.utc)


class AnalyticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("analytics-admin", is_superuser=True)
        cls.manager = User.objects.create_user("analytics-manager")
        cls.other = User.objects.create_user("analytics-other")
        cls.inactive = User.objects.create_user("analytics-inactive", is_active=False, is_superuser=True)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент аналитики")

    def setUp(self):
        self.client.force_login(self.manager)

    def deal(self, stage="new", amount="1.00", owner=None, at=None):
        record = Deal.objects.create(client=self.contact, title="Учебная сделка", stage=stage,
                                     amount=Decimal(amount), assigned_to=owner or self.manager)
        Deal.objects.filter(pk=record.pk).update(created_at=at or AT-timedelta(days=1))
        return record

    def interaction(self, kind="call", owner=None, at=None):
        return ClientInteraction.objects.create(client=self.contact, created_by=owner or self.manager,
                                               interaction_type=kind, subject="Учебное взаимодействие", occurred_at=at or AT-timedelta(days=1))

    def report(self, user=None, period=None):
        return build_report(user or self.manager, period or AnalyticsPeriod(), AT)

    def page(self, **params):
        with patch("crm.views.timezone.now", return_value=AT):
            return self.client.get(reverse("analytics"), params)

    def test_access_admin_manager_anonymous_inactive_and_read_only(self):
        for actor in (self.admin, self.manager):
            self.client.force_login(actor)
            self.assertEqual(self.page().status_code, 200)
            self.assertEqual(self.client.post(reverse("analytics")).status_code, 405)
        for actor in (None, self.inactive):
            self.client.logout()
            if actor:self.client.force_login(actor)
            self.assertRedirects(self.client.get(reverse("analytics")), "/login/?next=/analytics/")
        for actor in (AnonymousUser(), self.inactive):
            with self.assertRaises(PermissionDenied):build_report(actor, AnalyticsPeriod(), AT)

    def test_all_five_stages_partition_open_closed_and_conversion(self):
        for stage in Deal.Stage.values:self.deal(stage)
        self.deal("won");self.deal("won");self.deal("lost")
        data = self.report()["deals"]
        self.assertEqual(data["counts"], {"new":1,"contact":1,"proposal":1,"won":3,"lost":2})
        self.assertEqual((data["total"],data["open"],data["closed"]), (8,3,5))
        self.assertEqual(sum(row["count"] for row in data["rows"]), data["total"])
        self.assertEqual(data["conversion"], Decimal("60.0"))
        self.assertEqual(data["won_amount"], Decimal("3.00"))

    def test_empty_zero_money_and_no_conversion(self):
        response=self.page();data=response.context['report']
        self.assertEqual(data['deals']['total'],0)
        self.assertEqual(data['deals']['won_amount'],Decimal('0.00'))
        self.assertIsNone(data['deals']['conversion'])
        self.assertEqual(data['interactions']['total'],0)
        self.assertTrue(all(row['count']==0 for row in data['interactions']['rows']))
        self.assertContains(response,'Нет данных')
        self.assertContains(response,'0,00 ₽')

    def test_only_open_won_lost_and_rounding(self):
        self.deal('proposal')
        self.assertIsNone(self.report()['deals']['conversion'])
        win=self.deal('won')
        self.assertEqual(self.report()['deals']['conversion'],Decimal('100.0'))
        Deal.objects.filter(pk=win.pk).update(stage='lost')
        self.assertEqual(self.report()['deals']['conversion'],Decimal('0.0'))
        self.deal('won');self.deal('lost')
        self.assertEqual(self.report()['deals']['conversion'],Decimal('33.3'))
        self.assertEqual(closed_conversion(1,15),Decimal('6.3'))  # exact half: 6.25 -> 6.3, not half-even
        self.assertIsInstance(self.report()['deals']['conversion'],Decimal)

    def test_scoping_admin_manager_reassignment_and_no_foreign_money(self):
        own=self.deal('won','0.01');self.deal('won','9999999999.99',owner=self.other)
        self.interaction(owner=self.other)
        self.assertEqual(self.report()['deals']['won_amount'],Decimal('0.01'))
        self.assertEqual(self.report()['interactions']['total'],0)
        self.assertEqual(self.report(self.admin)['deals']['won_amount'],Decimal('10000000000.00'))
        Deal.objects.filter(pk=own.pk).update(assigned_to=self.other)
        self.assertEqual(self.report()['deals']['total'],0)
        self.assertEqual(self.report(self.other)['deals']['total'],2)

    def test_unknown_stage_is_diagnosed_without_inventing_sixth_column(self):
        self.deal('won');self.deal('not-a-stage')
        with self.assertLogs('crm.analytics',level='WARNING'):
            response=self.page()
        data=response.context['report']['deals']
        self.assertEqual((data['total'],data['invalid_count'],len(data['rows'])),(2,1,5))
        self.assertEqual(data['closed'],1)
        self.assertIsNone(data['conversion'])
        self.assertContains(response,'Конверсия недоступна')
        self.assertNotContains(response,'not-a-stage')

    def test_decimal_cents_only_won_and_upper_boundary(self):
        self.deal('won','0.01');self.deal('won','0.02');self.deal('lost','100.00');self.deal('proposal','200.00')
        self.assertEqual(self.report()['deals']['won_amount'],Decimal('0.03'))
        self.deal('won','9999999999.99')
        amount=self.report()['deals']['won_amount']
        self.assertIsInstance(amount,Decimal)
        self.assertEqual(amount,Decimal('10000000000.02'))

    def test_many_deals_stream_without_financial_sql_sum_or_n_plus_one(self):
        for _ in range(1001):self.deal('won','9999999999.99')
        for _ in range(20):self.interaction()
        request=RequestFactory().get(reverse('analytics'));request.user=self.manager
        with patch('crm.views.timezone.now',return_value=AT) as clock, CaptureQueriesContext(connection) as queries:
            response=analytics(request)
        self.assertEqual(response.status_code,200)
        self.assertEqual(clock.call_count,1)
        self.assertEqual(len(queries),5)
        sql=[q['sql'].upper() for q in queries]
        self.assertFalse(any('SUM(' in q for q in sql))
        money=[q for q in sql if '"AMOUNT"' in q]
        self.assertEqual(len(money),1)
        self.assertNotIn('JOIN',money[0])
        data=self.report()['deals']
        self.assertEqual(data['won_amount'],Decimal(999999999999*1001)/Decimal(100))
        self.assertEqual(data['counts']['won'],1001)

    def test_sqlite_orm_overflow_withholds_money_and_does_not_500(self):
        self.deal('won','0.01');self.deal('won','10000000000.00')
        with self.assertLogs('crm.analytics',level='ERROR') as logs:
            response=self.page()
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.context['report']['deals']['won_amount'])
        self.assertContains(response,'не может быть достоверно вычислен')
        self.assertNotContains(response,'0,01 ₽')
        self.assertNotContains(response,'InvalidOperation')
        self.assertNotContains(response,'Traceback')
        self.assertTrue(any('InvalidOperation' in message for message in logs.output))
        self.assertEqual(response.context['report']['deals']['total'],2)

    def test_sqlite_text_and_subcent_conversion_errors_are_caught(self):
        bad=self.deal('won')
        for raw in ('not-money','NaN','0.001'):
            with connection.cursor() as cursor:
                cursor.execute('UPDATE crm_deal SET amount=%s WHERE id=%s',[raw,bad.pk])
            with self.assertLogs('crm.analytics',level='ERROR'):
                response=self.page()
            self.assertEqual(response.status_code,200)
            self.assertIsNone(response.context['report']['deals']['won_amount'])

    def test_invalid_foreign_or_nonwon_money_is_not_read_for_manager(self):
        self.deal('won','10000000000.00',owner=self.other)
        self.deal('lost','10000000000.00')
        self.deal('won','0.10')
        self.assertEqual(self.report()['deals']['won_amount'],Decimal('0.10'))
        with self.assertLogs('crm.analytics',level='ERROR'):
            self.assertIsNone(self.report(self.admin)['deals']['won_amount'])

    def test_amount_validator_checks_range_scale_type_and_finiteness(self):
        for amount in (Decimal('0'),Decimal('-1'),Decimal('0.001'),Decimal('1.001'),Decimal('10000000000.00'),Decimal('NaN'),Decimal('Infinity'),0.1,'1.00'):
            with self.assertRaises(ValueError):validate_amount(amount)
        validate_amount(Decimal('0.01'));validate_amount(Decimal('9999999999.99'))

    def test_sqlite_orm_rounding_can_hide_raw_extra_decimal_places(self):
        record=self.deal('won')
        with connection.cursor() as cursor:
            cursor.execute('UPDATE crm_deal SET amount=%s WHERE id=%s',['1.234',record.pk])
        self.assertEqual(Deal.objects.get(pk=record.pk).amount,Decimal('1.23'))
        self.assertEqual(self.report()['deals']['won_amount'],Decimal('1.23'))
        # A documented limit, not proof that the original SQL value obeyed the two-place rule.

    def test_all_time_and_last30_start_boundary_and_future_cap(self):
        start=AT-timedelta(days=30)
        for instant in (start-timedelta(microseconds=1),start,AT,AT+timedelta(microseconds=1)):
            self.deal(at=instant);self.interaction(at=instant)
        self.assertEqual(self.report()['deals']['total'],3)
        response=self.page(period='30d')
        for key in ('deals','interactions'):
            self.assertEqual(response.context['report'][key]['total'],2)

    def test_custom_half_open_end_day_and_actual_date_fields(self):
        start=datetime(2026,10,1,tzinfo=dt_timezone.utc);end=datetime(2026,10,3,tzinfo=dt_timezone.utc)
        for instant in (start-timedelta(microseconds=1),start,end-timedelta(microseconds=1),end):
            self.deal(at=instant)
            record=self.interaction(at=instant)
            ClientInteraction.objects.filter(pk=record.pk).update(created_at=AT)
        response=self.page(period='custom',start_date='2026-10-01',end_date='2026-10-02')
        for key in ('deals','interactions'):
            self.assertEqual(response.context['report'][key]['total'],2)
        # Moving the current stage changes this cohort's conversion, not its creation-date eligibility.
        Deal.objects.filter(created_at=start).update(stage='won')
        self.assertEqual(self.page(period='custom',start_date='2026-10-01',end_date='2026-10-02').context['report']['deals']['counts']['won'],1)

    def test_invalid_dates_ranges_future_and_missing_period_withhold_metrics(self):
        self.deal('won')
        for params in ({'period':'bad'},{'period':'custom'}, {'period':'custom','start_date':'bad','end_date':'2026-10-01'},
                       {'period':'custom','start_date':'2026-10-02','end_date':'2026-10-01'},
                       {'period':'custom','start_date':'2026-10-01','end_date':'2026-02-30'},
                       {'period':'custom','start_date':'2099-01-01','end_date':'2099-01-02'},
                       {'period':'custom','start_date':'2026-01-01','end_date':'9999-12-31'}, {'start_date':'2026-01-01'}):
            response=self.page(**params)
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.context['form'].errors)
            self.assertIsNone(response.context['report']['deals'])
            self.assertIsNone(response.context['report']['interactions'])
            self.assertContains(response,'Показатели за период не рассчитаны')
            self.assertNotContains(response,'id="analytics-deal-total"')

    def test_custom_today_stops_at_now_including_current_instant(self):
        for instant in (AT,AT+timedelta(microseconds=1)):
            self.deal(at=instant);self.interaction(at=instant)
        response=self.page(period='custom',start_date='2026-10-26',end_date='2026-10-26')
        self.assertEqual(response.context['report']['deals']['total'],1)
        self.assertEqual(response.context['report']['interactions']['total'],1)

    def test_interaction_types_totals_scoping_and_unknown_diagnostic(self):
        for kind in ClientInteraction.Type.values:self.interaction(kind)
        self.interaction('email');self.interaction('meeting',owner=self.other)
        data=self.report()['interactions']
        self.assertEqual(data['counts'],{'call':1,'meeting':1,'email':2,'comment':1})
        self.assertEqual(data['total'],sum(data['counts'].values()))
        self.assertEqual(self.report(self.admin)['interactions']['total'],6)
        self.interaction('broken')
        with self.assertLogs('crm.analytics',level='WARNING'):
            data=self.report()['interactions']
        self.assertEqual((data['total'],data['invalid_count']),(6,1))

    def test_workload_reuses_overdue_scope_boundaries_not_period(self):
        own=ClientRequest.objects.create(client=self.contact,title='Своя заявка',assigned_to=self.manager,due_at=AT-timedelta(seconds=1))
        foreign=ClientRequest.objects.create(client=self.contact,title='Чужая заявка',assigned_to=self.other,due_at=AT-timedelta(days=2))
        for status,due in (('new',AT-timedelta(microseconds=1)),('new',AT),('completed',AT-timedelta(days=1)),('new',None)):
            Task.objects.create(request=own,title='Граница просрочки',assigned_to=self.manager,status=status,due_at=due)
        Task.objects.create(request=foreign,title='Рассогласованное назначение',assigned_to=self.manager,due_at=AT-timedelta(days=1))
        expected={'requests':{'total':1,'overdue':1},'tasks':{'total':4,'overdue':1}}
        response=self.page(period='custom',start_date='2020-01-01',end_date='2020-01-02')
        self.assertEqual(response.context['report']['workload'],expected)
        self.assertEqual(response.context['report']['deals']['total'],0)
        with patch('crm.views.timezone.now',return_value=AT):
            home=self.client.get(reverse('home')).context
        self.assertEqual((home['request_count'],home['task_count'],home['overdue_request_count'],home['overdue_task_count']),(1,4,1,1))
        self.assertEqual(self.report(self.admin)['workload']['tasks'],{'total':5,'overdue':2})

    def test_page_has_definitions_zero_percent_and_no_data_distinct(self):
        self.deal('lost')
        response=self.page()
        self.assertContains(response,'0,0%')
        self.assertContains(response,'Сумма успешно завершённых сделок')
        self.assertContains(response,'created_at')
        self.assertContains(response,'occurred_at')
        self.assertContains(response,'платежи система не хранит')
        self.assertContains(response,'независимо от периода')

    def test_invalid_filter_only_loads_workload_and_no_writes(self):
        before={model:list(model.objects.values()) for model in (Deal,ClientInteraction,Task,ClientRequest)}
        request=RequestFactory().get(reverse('analytics'),{'period':'bad'});request.user=self.manager
        with patch('crm.views.timezone.now',return_value=AT), CaptureQueriesContext(connection) as queries:
            response=analytics(request)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(queries),2)
        self.assertTrue(all(q['sql'].startswith('SELECT') for q in queries))
        for model,rows in before.items():self.assertEqual(list(model.objects.values()),rows)
