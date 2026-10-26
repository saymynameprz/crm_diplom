from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .analytics_presentation import distribution_chart, percentage, present_report, visual_row
from .models import Client, ClientInteraction, ClientRequest, Deal, Task
from .views import analytics

AT = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


class ChartMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids=[]; self.references=[]; self.styles=[]; self.labels=[]; self.controls=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if 'id' in attrs:self.ids.append(attrs['id'])
        for name in ('aria-labelledby','aria-describedby'):
            self.references.extend(attrs.get(name,'').split())
        if 'style' in attrs:self.styles.append(attrs['style'])
        if tag=='label':self.labels.append(attrs['for'])
        if tag in ('input','select','textarea') and 'id' in attrs:self.controls.append(attrs['id'])


class ChartPresentationTests(SimpleTestCase):
    def test_safe_percentages_and_ascii_width_not_arbitrary_style(self):
        for count,total,value in ((1,3,Decimal('33.3')),(3,3,Decimal('100.0')),(0,3,Decimal('0.0')),(1,16,Decimal('6.3'))):
            self.assertEqual(percentage(count,total),value)
            row=visual_row({'count':count},total)
            self.assertEqual(Decimal(row['width']),value)
            self.assertRegex(row['width'],r'^\d{1,3}\.\d$')
        for count,total in ((0,0),(-1,3),(4,3),(1,-1),(True,2),(1,False),('1; color:red',3),(1,'NaN'),(Decimal('Infinity'),2),(1,None)):
            self.assertIsNone(percentage(count,total))
            self.assertEqual(visual_row({'count':count},total)['width'],'0.0')

    def test_large_integer_counts_stay_finite_and_bounded(self):
        for count,total in ((10**50,10**50),(10**40,10**50),(0,10**50)):
            row=visual_row({'count':count},total)
            self.assertTrue(Decimal(row['width']).is_finite())
            self.assertTrue(Decimal(0)<=Decimal(row['width'])<=Decimal(100))

    def test_empty_invalid_groups_and_no_mutation_without_database(self):
        group={'total':0,'invalid_count':0,'rows':[{'value':'new','label':'Новая','count':0}]}
        original=deepcopy(group)
        self.assertEqual(distribution_chart(group)['state'],'empty')
        self.assertEqual(group,original)
        for changed in ({'total':1,'invalid_count':1},{'total':-1},{'total':'unsafe'},{'total':False},
                        {'rows':[{'value':'new','label':'Новая','count':'unsafe'}]}):
            self.assertEqual(distribution_chart({**group,**changed})['state'],'invalid')
        self.assertIsNone(distribution_chart(None))


class AnalyticsChartTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager=User.objects.create_user('chart-manager')
        cls.other=User.objects.create_user('chart-other')
        cls.admin=User.objects.create_user('chart-admin',is_superuser=True)
        cls.contact=Client.objects.create(full_name='Вымышленный клиент диаграмм')

    def setUp(self):self.client.force_login(self.manager)

    def deal(self,stage='new',amount='1.00',owner=None,at=None):
        record=Deal.objects.create(client=self.contact,title='Учебная сделка',stage=stage,amount=Decimal(amount),assigned_to=owner or self.manager)
        Deal.objects.filter(pk=record.pk).update(created_at=at or AT-timedelta(days=1))
        return record

    def interaction(self,kind='call',owner=None,at=None):
        return ClientInteraction.objects.create(client=self.contact,created_by=owner or self.manager,interaction_type=kind,subject='Учебное событие',occurred_at=at or AT-timedelta(days=1))

    def page(self,**params):
        with patch('crm.views.timezone.now',return_value=AT):return self.client.get(reverse('analytics'),params)

    def test_real_distribution_counts_order_percentages_and_tables(self):
        for stage in ['new','contact','proposal','won','won','won','lost','lost']:self.deal(stage)
        for kind in ['call','call','meeting','email','email','email','comment']:self.interaction(kind)
        response=self.page();report=response.context['report'];charts=response.context['charts']
        self.assertEqual([row['value'] for row in charts['deals']['rows']],Deal.Stage.values)
        self.assertEqual([row['value'] for row in charts['interactions']['rows']],ClientInteraction.Type.values)
        self.assertEqual([row['percentage'] for row in charts['deals']['rows']],[Decimal('12.5')]*3+[Decimal('37.5'),Decimal('25.0')])
        self.assertEqual([row['percentage'] for row in charts['interactions']['rows']],[Decimal('28.6'),Decimal('14.3'),Decimal('42.9'),Decimal('14.3')])
        for key in ('deals','interactions'):
            self.assertEqual([row['count'] for row in charts[key]['rows']],[row['count'] for row in report[key]['rows']])
        self.assertContains(response,'id="deals-chart"');self.assertContains(response,'id="interactions-chart"')
        self.assertContains(response,'style="width: 37.5%"')
        self.assertContains(response,'37,5%')
        self.assertContains(response,'aria-label="Распределение сделок по этапам"')
        self.assertContains(response,'aria-label="Распределение взаимодействий по типам"')
        self.assertEqual(report['deals']['conversion'],Decimal('60.0'))

    def test_one_stage_and_type_are_full_other_rows_zero(self):
        for _ in range(3):self.deal('won');self.interaction('comment')
        response=self.page()
        for key,winner in (('deals','won'),('interactions','comment')):
            for row in response.context['charts'][key]['rows']:
                self.assertEqual(row['width'],'100.0' if row['value']==winner else '0.0')
                self.assertEqual(row['count'],3 if row['value']==winner else 0)
        self.assertContains(response,'100,0%')

    def test_empty_data_has_safe_bars_no_conversion_and_text(self):
        response=self.page();charts=response.context['charts']
        for key in ('deals','interactions'):
            self.assertEqual(charts[key]['state'],'empty')
            self.assertTrue(all(row['count']==0 and row['width']=='0.0' and row['percentage'] is None for row in charts[key]['rows']))
        self.assertContains(response,'Нет сделок за выбранный период')
        self.assertContains(response,'Нет взаимодействий за выбранный период')
        self.assertContains(response,'Нет данных')
        self.assertContains(response,'0,00 ₽')
        self.assertNotContains(response,'NaN');self.assertNotContains(response,'Infinity')

    def test_invalid_period_withholds_period_figures_not_current_workload(self):
        self.deal('won')
        for params in ({'period':'custom','start_date':'bad','end_date':'2026-01-01'},
                       {'period':'custom','start_date':'2026-10-02','end_date':'2026-10-01'}):
            response=self.page(**params)
            self.assertIsNone(response.context['charts']['deals'])
            self.assertIsNone(response.context['charts']['interactions'])
            self.assertNotContains(response,'id="deals-chart"');self.assertNotContains(response,'id="interactions-chart"')
            self.assertContains(response,'id="workload-chart"')
            self.assertContains(response,'Показатели за период не рассчитаны')

    def test_periods_preserve_boundaries_and_source_fields(self):
        self.deal('new',at=AT-timedelta(days=60));self.interaction('call',at=AT-timedelta(days=60))
        self.deal('won',at=datetime(2026,10,2,23,59,59,999999,tzinfo=timezone.utc))
        self.deal('lost',at=datetime(2026,10,3,tzinfo=timezone.utc))
        event=self.interaction('email',at=datetime(2026,10,2,23,59,59,999999,tzinfo=timezone.utc))
        ClientInteraction.objects.filter(pk=event.pk).update(created_at=AT)
        self.assertEqual(self.page().context['charts']['deals']['total'],3)
        response=self.page(period='30d')
        self.assertEqual(response.context['charts']['deals']['total'],2)
        self.assertContains(response,'Последние 30 суток (30×24 часа)')
        response=self.page(period='custom',start_date='2026-10-01',end_date='2026-10-02')
        for key in ('deals','interactions'):self.assertEqual(response.context['charts'][key]['total'],1)
        self.assertEqual(response.context['charts']['deals']['rows'][3]['width'],'100.0')
        self.assertContains(response,'01.10.2026 — 02.10.2026')
        self.assertContains(response,'created_at');self.assertContains(response,'occurred_at')

    def test_current_overdue_shares_use_current_totals_not_period(self):
        own=ClientRequest.objects.create(client=self.contact,title='Своя заявка',assigned_to=self.manager,due_at=AT-timedelta(days=1))
        foreign=ClientRequest.objects.create(client=self.contact,title='Чужая заявка',assigned_to=self.other,due_at=AT-timedelta(days=1))
        for due in (AT-timedelta(days=1),AT,None):Task.objects.create(request=own,title='Своя задача',assigned_to=self.manager,due_at=due)
        Task.objects.create(request=foreign,title='Рассогласованная',assigned_to=self.manager,due_at=AT-timedelta(days=1))
        response=self.page(period='custom',start_date='2020-01-01',end_date='2020-01-02')
        rows=response.context['charts']['workload']
        self.assertEqual([(row['count'],row['total'],row['width']) for row in rows],[(1,1,'100.0'),(1,3,'33.3')])
        self.assertContains(response,'Текущее состояние на момент открытия страницы')
        self.assertEqual(rows,self.page().context['charts']['workload'])

    def test_unknown_stage_and_type_show_warning_without_complete_chart(self):
        self.deal('won');self.deal('invalid-stage');self.interaction('invalid-type')
        with self.assertLogs('crm.analytics',level='WARNING'):response=self.page()
        self.assertEqual(response.context['charts']['deals']['state'],'invalid')
        self.assertEqual(response.context['charts']['interactions']['state'],'invalid')
        self.assertNotContains(response,'id="deals-chart"');self.assertNotContains(response,'id="interactions-chart"')
        self.assertContains(response,'Конверсия недоступна');self.assertContains(response,'Полная диаграмма не построена')
        self.assertContains(response,'id="analytics-stage-won">1')
        self.assertNotContains(response,'invalid-stage');self.assertNotContains(response,'invalid-type')

    def test_money_and_conversion_keep_decimal_values_beyond_single_deal_limit(self):
        self.deal('won','9999999999.99');self.deal('won','9999999999.99');self.deal('lost','0.01')
        response=self.page();report=response.context['report']
        self.assertEqual(report['deals']['won_amount'],Decimal('19999999999.98'))
        self.assertEqual(report['deals']['conversion'],Decimal('66.7'))
        self.assertContains(response,'19999999999,98 ₽')
        self.assertContains(response,'66,7%')
        before=deepcopy(report)
        with self.assertNumQueries(0):present_report(report)
        self.assertEqual(report,before)

    def test_corrupt_money_is_unavailable_while_count_diagram_remains_valid(self):
        self.deal('won','0.01');self.deal('won','10000000000.00')
        with self.assertLogs('crm.analytics',level='ERROR'):response=self.page()
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.context['report']['deals']['won_amount'])
        self.assertEqual(response.context['charts']['deals']['rows'][3]['width'],'100.0')
        self.assertContains(response,'Финансовый показатель не может быть достоверно вычислен')
        self.assertNotContains(response,'0,00 ₽');self.assertNotContains(response,'0,01 ₽')
        self.assertNotContains(response,'InvalidOperation');self.assertNotContains(response,'Traceback')

    def test_scoped_charts_and_attributes_never_include_foreign_counts(self):
        for _ in range(2):self.deal('won');self.interaction('call')
        Deal.objects.bulk_create([Deal(client=self.contact,assigned_to=self.other,title='Чужая',stage='lost',amount=Decimal('1.00')) for _ in range(137)])
        Deal.objects.filter(assigned_to=self.other).update(created_at=AT-timedelta(days=1))
        ClientInteraction.objects.bulk_create([ClientInteraction(client=self.contact,created_by=self.other,subject='Чужое',interaction_type='email',occurred_at=AT-timedelta(days=1)) for _ in range(139)])
        response=self.page();charts=response.context['charts']
        self.assertEqual((charts['deals']['total'],charts['interactions']['total']),(2,2))
        self.assertEqual(charts['deals']['rows'][3]['width'],'100.0')
        self.assertEqual(charts['deals']['rows'][4]['count'],0)
        self.assertNotContains(response,'137');self.assertNotContains(response,'139')
        self.client.force_login(self.admin);response=self.page()
        self.assertEqual((response.context['charts']['deals']['total'],response.context['charts']['interactions']['total']),(139,141))
        self.client.logout();self.assertRedirects(self.client.get(reverse('analytics')),'/login/?next=/analytics/')

    def test_query_count_stays_five_for_large_report_two_for_invalid_filter(self):
        Deal.objects.bulk_create([Deal(client=self.contact,assigned_to=self.manager,title='Много',stage='won',amount=Decimal('0.01')) for _ in range(1001)])
        Deal.objects.update(created_at=AT-timedelta(days=1))
        ClientInteraction.objects.bulk_create([ClientInteraction(client=self.contact,created_by=self.manager,subject='Много',interaction_type='comment',occurred_at=AT-timedelta(days=1)) for _ in range(501)])
        for data,expected in (({},5),({'period':'bad'},2)):
            request=RequestFactory().get(reverse('analytics'),data);request.user=self.manager
            with patch('crm.views.timezone.now',return_value=AT) as clock,CaptureQueriesContext(connection) as queries:
                response=analytics(request)
            self.assertEqual(response.status_code,200)
            self.assertEqual(len(queries),expected,[q['sql'] for q in queries])
            self.assertEqual(clock.call_count,1)
            self.assertFalse(any('SUM(' in q['sql'].upper() for q in queries))

    def test_chart_markup_is_named_numeric_and_css_is_page_specific(self):
        self.deal('contact');self.interaction('meeting')
        response=self.page();markup=ChartMarkup();markup.feed(response.content.decode())
        self.assertEqual(len(markup.ids),len(set(markup.ids)))
        self.assertTrue(all(ref in markup.ids for ref in markup.references))
        self.assertTrue(all(label in markup.controls for label in markup.labels))
        self.assertEqual(len(markup.styles),11)
        for style in markup.styles:
            self.assertRegex(style,r'^width: \d{1,3}\.\d%$')
            self.assertTrue(Decimal('0')<=Decimal(re.search(r'[\d.]+',style)[0])<=Decimal('100'))
        self.assertContains(response,'aria-hidden="true"',count=11)
        self.assertContains(response,'caption>Точные значения')
        self.assertContains(response,'crm/analytics.css')
        self.assertNotContains(self.client.get(reverse('home')),'crm/analytics.css')
