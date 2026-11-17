"""Rebuild print layouts from local geometry; stdlib only, no network/browser.
Mermaid files preserve portable logic; this file preserves the composed SVG layouts.
"""
from pathlib import Path
from html import escape
import textwrap
import re

ROOT = Path(__file__).resolve().parent

class Drawing:
    def __init__(self, title, w=720, h=1000):
        self.w, self.h = w, h
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{165 if w == 720 else 245}mm" height="{h / w * (165 if w == 720 else 245):.2f}mm" viewBox="0 0 {w} {h}" role="img" aria-labelledby="title desc">',
          f'<title id="title">{escape(title)}</title><desc id="desc">Чёрно-белая схема для ВКР. Редактируемая логика — одноимённый Mermaid; точная компоновка — render_svg.py.</desc>',
          '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#000"/></marker></defs>',
          '<rect width="100%" height="100%" fill="#fff"/>']
    def rect(self, x, y, w, h, fill='#fff', dash=False, rx=0):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="#000" stroke-width="1.4"'+(' stroke-dasharray="6 4"' if dash else '')+'/>')
    def text(self,x,y,lines,size=16,bold=False,anchor='middle',gap=None):
        if isinstance(lines,str): lines=lines.split('\n')
        gap = gap or size*1.25
        self.parts.append(f'<text x="{x}" y="{y}" fill="#000" font-family="DejaVu Sans, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" text-anchor="{anchor}">')
        for i,line in enumerate(lines): self.parts.append(f'<tspan x="{x}" dy="{0 if i == 0 else gap}">{escape(line)}</tspan>')
        self.parts.append('</text>')
    def box(self,x,y,w,h,lines,kind='process',size=16):
        self.parts.append('<g class="node-block">')
        if kind=='condition':
            self.parts.append(f'<path d="M{x+18} {y}H{x+w-18}L{x+w} {y+h/2}L{x+w-18} {y+h}H{x+18}L{x} {y+h/2}Z" fill="#f5f5f5" stroke="#000" stroke-width="1.4"/>')
        else: self.rect(x,y,w,h,rx=16 if kind=='end' else 0)
        lines=lines.split('\n') if isinstance(lines,str) else lines
        self.text(x+w/2,y+h/2-(len(lines)-1)*size*1.25/2+size*.34,lines,size)
        self.parts.append('</g>')
    def edge(self,points,label=None,dash=False,labelpos=None):
        self.parts.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in points)+'" fill="none" stroke="#000" stroke-width="1.4" marker-end="url(#arrow)"'+(' stroke-dasharray="5 4"' if dash else '')+'/>')
        if label:
            x,y=labelpos or points[len(points)//2]; self.label(x,y,label)
    def label(self,x,y,text,size=15):
        w=max(len(t) for t in text.split('\n'))*size*.62+10;h=len(text.split('\n'))*size*1.25+4
        self.parts.append(f'<rect x="{x-w/2}" y="{y-size}" width="{w}" height="{h}" fill="#fff"/>');self.text(x,y,text,size)
    def save(self,name):
        if hasattr(self, 'y'):
            final_h = self.y + 18
            self.parts[0] = self.parts[0].replace(f'viewBox="0 0 {self.w} {self.h}"', f'viewBox="0 0 {self.w} {final_h}"').replace(f'height="{self.h / self.w * 165:.2f}mm"', f'height="{final_h / self.w * 165:.2f}mm"')
        if hasattr(self, 'last_note_top'):
            self.parts = [re.sub(r'y2="[^"]+"', f'y2="{self.last_note_top-8}"', part) if 'class="lifeline"' in part else part for part in self.parts]
        (ROOT/name).write_text('\n'.join(self.parts+['</svg>'])+'\n')


def architecture():
    d=Drawing('Р1. Архитектура CRM',1080,700)
    d.rect(175,45,720,540,fill='#f5f5f5')
    d.text(535,69,'Единое приложение Django',18,True)
    for a in [(15,100,140,65,'Браузер\nHTTP'),(215,100,175,65,'Маршруты CRM\nи админ-панели'),(455,100,185,65,'Представления\nправа и вызовы'),(715,100,160,65,'Шаблоны\nHTML-страницы'),
      (215,245,175,75,'Формы\nпроверка входа'),(455,245,185,75,'Аналитические\nвычисления'),(715,245,160,75,'Подготовка и\nподтверждение'),
      (215,415,175,85,'Математическое\nядро: два метода\nи проверка плана'),(455,415,185,85,'Отображение\nаналитики\nи распределения'),(715,415,160,85,'Сервис заявки\nи автозадачи'),
      (455,530,420,40,'Модели и ORM'),(925,525,140,55,'SQLite')]:d.box(*a)
    d.edge([(155,132),(215,132)])
    d.edge([(390,132),(455,132)])
    d.edge([(640,132),(715,132)])
    d.edge([(610,165),(610,180),(970,180),(970,390),(830,390),(830,415)],'создание заявки',labelpos=(840,181))
    d.edge([(390,480),(455,480)],'данные',True,(422,471))
    d.edge([(795,100),(795,20),(85,20),(85,100)],'HTML-ответ',True,(440,23))
    d.edge([(490,165),(490,205),(302,205),(302,245)])
    d.edge([(548,165),(548,245)])
    d.edge([(610,165),(610,205),(795,205),(795,245)])
    d.edge([(302,320),(302,415)],'данные через\nпредставление',True,(302,357))
    d.edge([(548,320),(548,415)],'значения',True,(548,370))
    d.edge([(795,320),(795,415)])
    # The snapshot module calls the pure plan checker; a bridge marks a crossing.
    d.parts.append('<path d="M755 320V385H560 Q548 367 536 385 H427 Q415 367 403 385 H405V455H390" fill="none" stroke="#000" stroke-width="1.4" marker-end="url(#arrow)"/>')
    d.label(625,386,'проверка плана',15)
    d.edge([(390,280),(415,280),(415,550),(455,550)])
    d.edge([(640,280),(680,280),(680,530)])
    d.edge([(875,280),(905,280),(905,513),(850,513),(850,530)])
    d.edge([(795,500),(795,530)])
    d.edge([(875,550),(925,550)])
    d.edge([(640,452),(700,452),(700,175),(795,175),(795,165)],'отображение',True,(703,363))
    d.text(30,615,['Сплошные стрелки — обработка / доступ к данным; пунктир — данные и отображение.',
      'Формы готовят задачу; расчёт запускает представление. Ядро не обращается к SQLite.',
      'Обычные изменения могут идти из форм в ORM. Авторизация входит в Django.',
      'Пересечения без точки и с дугой не соединяют линии; фоновых процессов нет.'],15,anchor='start')
    d.save('R1_architecture.svg')


def entities():
    d=Drawing('Р2. Связи сущностей и ограничения',1080,700)
    data=[(420,25,240,60,'Пользователь'),(30,220,220,65,'Клиент'),(420,220,240,65,'Заявка'),(840,220,210,65,'Сделка'),(30,440,250,65,'Взаимодействие'),(420,440,240,65,'Задача'),(790,435,270,90,'Подтверждение\nраспределения\nУникальный ID')]
    for a in data:d.box(*a)
    # Cardinalities read as: parent endpoint, child endpoint.
    d.edge([(540,85),(540,220)],'0..1 : 0..N\nответственный / ОЧ',labelpos=(540,149))
    d.edge([(600,85),(600,130),(945,130),(945,220)],'0..1 : 0..N / ОЧ',labelpos=(790,132))
    d.edge([(660,55),(1070,55),(1070,480),(1060,480)],'1 : 0..N / ЗУ',labelpos=(952,57))
    d.edge([(480,85),(480,130),(300,130),(300,472),(280,472)],'1 : 0..N\nавтор / ЗУ',labelpos=(365,132))
    d.edge([(660,70),(745,70),(745,472),(660,472)],'0..1 : 0..N\nответственный / ОЧ',labelpos=(745,352))
    d.edge([(250,252),(420,252)],'1 : 0..N / ЗУ',labelpos=(334,248))
    d.edge([(140,285),(140,440)],'1 : 0..N\nклиент / ЗУ',labelpos=(140,356))
    d.parts.append('<path d="M205 285V400H288 Q300 382 312 400 H528 Q540 382 552 400 H733 Q745 382 757 400 H975V285" fill="none" stroke="#000" stroke-width="1.4" marker-end="url(#arrow)"/>')
    d.label(415,400,'1 : 0..N / клиент / ЗУ')
    d.edge([(540,285),(540,440)],'1 : 0..N\nзаявка / ЗУ',labelpos=(540,354))
    d.text(30,565,['Кратности: родитель для одной дочерней записи : дочерние для одного родителя.',
      '1 — родитель обязателен; 0..1 — необязателен; 0..N — любое число дочерних записей.',
      'ОЧ — при удалении пользователя ссылка очищается. ЗУ — удаление родителя запрещено.',
      'ОЧ / ЗУ описывают on_delete Django; не обещают отдельный SQL ON DELETE.',
      'Не более одной автоматической задачи на заявку; ручных задач может быть несколько.',
      'Прямой связи задачи со сделкой нет. Список созданных ID подтверждения — JSON, не FK.',
      'Квалификации, ставки и резервы часов — входы расчёта, не сущности хранения.'],15,anchor='start')
    d.save('R2_entities.svg')


def greedy():
    d=Drawing('Р3а. Жадный алгоритм',720,900)
    for args in [(190,15,335,50,'А. Жадный алгоритм\nобщая модель из 2.2','end'),(190,85,335,55,'Проверенный поддерживаемый вход;\nзаявки и сотрудники — по ID','process'),
      (235,165,245,50,'Пустой пакет?','condition'),(545,160,165,75,'Оптимальный\nпустой план','end'),(235,240,245,50,'Все заявки назначены?','condition'),(10,230,165,100,'Проверить план\nи метрики.\nДопустимый план.\nОптимальность\nне доказана','end',15),
      (190,320,335,42,'Взять следующую заявку','process'),(190,385,335,70,'Квалификация и остаток мощности:\nотобрать допустимых исполнителей\nпо условию (2.11)','process'),(235,480,245,50,'Есть кандидаты?','condition'),(545,465,165,105,'Неудача\nэвристики:\nневыполнимость\nне доказана','end'),
      (190,560,335,55,'Точные приращения той же F\nдля всех кандидатов: (2.12)','process'),(190,640,335,55,'Минимум приращения;\nпри равенстве — меньший ID','process'),(190,720,335,65,'Закрепить назначение.\nОбновить часы и максимум загрузки.\nБез возврата','process')]:d.box(*args)
    for a,b in [(65,85),(140,165),(215,240),(290,320),(362,385),(455,480),(530,560),(615,640),(695,720)]:d.edge([(357,a),(357,b)])
    d.edge([(480,190),(545,190)],'Да',labelpos=(510,184))
    d.edge([(235,265),(175,265)],'Да',labelpos=(205,258))
    d.edge([(480,505),(545,505)],'Нет',labelpos=(510,496))
    d.edge([(357,785),(357,808),(5,808),(5,220),(260,220),(260,240)])
    d.label(380,232,'Нет');d.label(380,310,'Нет');d.label(382,550,'Да')
    d.text(30,835,['Некорректный ввод и неподдерживаемые гарантии отклоняются до поиска.',
      'Пустая квалификационная строка даёт ранний отказ эвристики:',
      'это показано как отсутствие кандидатов.'],15,anchor='start')
    d.save('R3a_greedy.svg')


def branch_bound():
    d=Drawing('Р3б. Метод ветвей и границ',720,1020)
    for args in [(195,15,330,55,'Б. Ветви и границы\nНепустой вход общей модели','end'),(230,90,260,60,'Структурная\nневыполнимость?','condition'),(10,85,165,85,'Невыполнимость\nдоказана','end'),
     (195,180,330,65,'Полный жадный план — граница.\nНет плана — искать без неё.\nНачать с корня','process'),(230,270,260,55,'Есть ещё узел DFS\nдля входа?','condition'),(10,255,165,120,'Дерево пройдено:\nЕсть план —\nоптимум доказан.\nНет плана —\nневыполнимость\nдоказана','end',15),
     (230,360,260,55,'Лимит мешает\nвойти в узел?','condition'),(550,345,160,125,'План есть:\nдопустим, без\nдоказанного\nоптимума.\nНет плана:\nнеопределённость','end',15),
     (230,450,260,60,'Учесть посещение.\nПолный вектор?','condition'),(550,485,160,105,'Пересчитать\nметрики.\nВыбор по F,\nзатем вектору ID','process',15),
     (195,550,330,45,'Нижняя оценка B по (2.14)','process'),(215,625,285,65,'Нет продолжения или B\nстрого больше лучшей F?','condition'),
     (195,735,330,80,'Квалификация и мощность.\nПорядок: приращение F, затем ID.\nНазначить и спуститься.','process'),
     (195,870,330,80,'Возврат к родителю, если есть.\nСнять назначение, вернуть часы.\nИскать кандидата и у предков.','process')]:d.box(*args)
    for a,b in [(70,90),(150,180),(245,270),(325,360),(415,450),(510,550),(595,625),(690,735)]:d.edge([(360,a),(360,b)])
    d.edge([(230,120),(175,120)],'Да',labelpos=(202,112))
    d.edge([(230,296),(175,296)],'Нет',labelpos=(202,289))
    d.parts.append('<path d="M490 388H525 Q535 372 545 388H550" fill="none" stroke="#000" stroke-width="1.4" marker-end="url(#arrow)"/>');d.label(512,350,'Да')
    d.parts.append('<path d="M490 480H525 Q535 464 545 480H630V485" fill="none" stroke="#000" stroke-width="1.4" marker-end="url(#arrow)"/>');d.label(507,467,'Да')
    d.edge([(630,590),(630,844),(480,844),(480,870)])
    d.edge([(215,658),(30,658),(30,910),(195,910)],'Да',labelpos=(140,650))
    d.edge([(525,775),(535,775),(535,305),(490,305)])
    d.parts.append('<circle cx="535" cy="775" r="3" fill="#000"/>')
    d.edge([(525,910),(535,910),(535,305),(490,305)])
    for x,y,t in [(386,172,'Нет'),(385,350,'Да'),(385,440,'Нет'),(385,540,'Нет'),(385,720,'Нет')]:d.label(x,y,t)
    d.text(30,973,['B = лучшей F: не отсекать. Лимит проверяется только перед новым входом.',
      'Исчерпание ровно на лимите сохраняет доказательство;',
      'глобальный разрыв оптимальности не вычисляется.'],15,anchor='start')
    d.save('R3b_branch_bound.svg')


class Sequence(Drawing):
    def __init__(self,title,names,h):
        super().__init__(title,720,h);self.names=names;self.xs=[70+580*i/(len(names)-1) for i in range(len(names))];self.y=100
        for x,name in zip(self.xs,names):
            self.box(x-67,15,134,60,name,size=14)
            self.parts.append(f'<line class="lifeline" x1="{x}" y1="75" x2="{x}" y2="{h-120}" stroke="#000" stroke-width="1" stroke-dasharray="4 5"/>')
    def msg(self,a,b,text,reply=False):
        lines=textwrap.wrap(text,64,break_long_words=False);height=34+max(0,len(lines)-1)*18
        x1,x2=self.xs[a],self.xs[b];y=self.y+len(lines)*18
        if a==b:
            self.edge([(x1,y-8),(x1+26,y-8),(x1+26,y+5),(x1,y+5)],dash=reply)
        else:self.edge([(x1,y),(x2,y)],dash=reply)
        width=max(len(t) for t in lines)*10.2+12;center=max(width/2+8,min(712-width/2,(x1+x2)/2))
        self.parts.append(f'<rect x="{center-width/2}" y="{self.y-13}" width="{width}" height="{len(lines)*18}" fill="#fff"/>')
        self.text(center,self.y,lines,15,gap=18);self.y+=height
    def frame(self, top, bottom, divider=None):
        self.rect(3,top,714,bottom-top,fill='none')
        if divider is not None:
            self.parts.append(f'<line x1="3" y1="{divider}" x2="717" y2="{divider}" stroke="#000" stroke-dasharray="5 4"/>')
    def banner(self,text):
        self.rect(8,self.y-13,704,26,fill='#f5f5f5');self.text(360,self.y+5,text,15,True);self.y+=36
    def note(self,text):
        self.last_note_top=self.y-13
        lines=textwrap.wrap(text,73,break_long_words=False);h=len(lines)*18+12
        self.rect(8,self.y-13,704,h,fill='#fff');self.text(360,self.y+5,lines,15,gap=18);self.y+=h+10


def sequence_a():
    d=Sequence('Р4а. Расчёт и предпросмотр',['Администратор','Веб-обработка\nи формы','Ядро\nрасчёта','Подготовка\nснимка'],1010)
    d.msg(0,1,'Ввод экспертных оценок')
    d.msg(1,1,'Права, проверка формы и подготовка задачи')
    d.msg(1,2,'Расчёт обоими методами')
    d.msg(2,1,'Планы, точные показатели и доказанность',True)
    d.banner('Полного допустимого плана ветвей/границ нет → подготовка недоступна')
    d.banner('Есть полный допустимый план ветвей/границ → продолжить')
    d.msg(1,3,'Проверить готовый план и выдать подписанный снимок')
    d.note('Исходные 15 минут начинаются при выдаче снимка после расчёта. Подпись не шифрует данные и не доказывает экспертные оценки.')
    d.msg(3,1,'Подписанные входы, план, автор и идентификатор',True)
    d.msg(1,0,'Рекомендация и переход к деталям',True)
    d.msg(0,1,'Клиенты, описания, горизонт, общий срок; подтверждение оценок')
    d.msg(1,3,'Проверка подписи, исходного срока и актуальности сотрудников')
    d.msg(3,2,'Проверка готового назначения и точных метрик, без поиска')
    d.msg(2,3,'Проверенный план',True)
    d.msg(1,3,'Проверить детали и формы заявок; подписать подготовленный снимок')
    d.msg(3,1,'Подготовленный снимок; исходное время выдачи сохранено',True)
    d.msg(1,0,'Предварительный просмотр без создания записей',True)
    d.note('Расчёт и предпросмотр не создают заявок, задач или подтверждения. Предпросмотр не продлевает исходные 15 минут. Явное финальное согласие и запись показаны в Р4б.')
    d.save('R4a_calculation_preview.svg')


def sequence_b():
    d=Sequence('Р4б. Подтверждение, повтор и атомарное сохранение',['Администратор','Веб-обработка\nи формы','Сохранение\nи сервис\nзаявки','Проверка\nплана в ядре','Модели /\nSQLite'],1170)
    d.msg(0,1,'Явное согласие на создание; отдельно — на недоказанный план')
    d.msg(1,2,'Тот же подготовленный снимок')
    d.msg(2,2,'Подпись, автор, действующие права и согласия')
    d.msg(2,4,'Поиск ID подтверждения, сверка автора и отпечатка')
    outer_top=d.y-13
    d.banner('Повтор завершённого пакета: тот же ID, автор и отпечаток')
    d.msg(4,2,'Сохранённые идентификаторы, даже после исходных 15 минут',True)
    d.msg(2,1,'Прежний результат без новых записей',True)
    outer_split=d.y-14
    d.banner('Иначе: новое подтверждение. Иной автор / отпечаток с тем же ID → отказ')
    d.msg(2,2,'Исходные 15 минут и актуальность сотрудников')
    d.msg(2,3,'Проверка готового плана и точных метрик, без поиска')
    d.msg(3,2,'Проверенный план',True)
    d.banner('Начало одной транзакции')
    d.msg(2,4,'Первая запись — занять уникальный ID подтверждения')
    d.msg(2,2,'Повторить права, сроки, сотрудников, клиентов и формы')
    d.msg(2,3,'Повторная проверка готового назначения и метрик')
    d.msg(3,2,'Результат проверки',True)
    d.msg(2,4,'Для каждой строки: заявка + автозадача; проверить связи, состояния и срок')
    d.msg(2,2,'Финальная проверка исходных 15 минут и срока обработки')
    d.y+=20
    alt_top=d.y-13
    d.text(14,alt_top-5,'Альтернатива исходов',15,True,anchor='start')
    d.msg(2,4,'Успех: записать созданные ID и COMMIT всей транзакции')
    alt_split=d.y-14
    d.msg(2,4,'Ошибка внутри транзакции: ROLLBACK заявок, задач и подтверждения')
    d.frame(alt_top,d.y-8,alt_split)
    d.banner('Конец транзакции: один из двух исходов выше')
    d.msg(2,1,'Результат либо ошибка',True)
    d.frame(outer_top,d.y-8,outer_split)
    d.msg(1,0,'Показать результат',True)
    d.note('Ошибка до записи → отказ без новых заявок. Сбой ответа после COMMIT не откатывает пакет: повторить то же подтверждение. Конкурентный конфликт ID → прочитать совпавший результат; занятость SQLite → повторить то же действие.')
    d.save('R4b_commit_replay.svg')


if __name__=='__main__':
    architecture();entities();greedy();branch_bound();sequence_a();sequence_b()
