"""Signed recommendations and one atomic, idempotent transition into CRM records."""
from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from .forms import (ClientRequestForm, OptimizationForm, OptimizationSizeForm,
                    OptimizationDetailsForm, OPTIMIZATION_SNAPSHOT_LIMIT)
from .models import Client, OptimizationCommit, Task
from .optimization import Status, Result, evaluate_assignment, InputError
from .services import create_request_with_task


SALT = "crm.optimization.confirm.v1"
SNAPSHOT_TTL = 15 * 60
ALLOWED = (Status.OPTIMAL.value, Status.FEASIBLE_NOT_PROVEN.value)
METRICS = ("cost", "utilization", "labor_hours", "objective", "cost_scale")


class CommitError(Exception):
    def __init__(self, message, status=409, retry=False):
        super().__init__(message)
        self.message, self.status, self.retry = message, status, retry


@dataclass(frozen=True)
class VerifiedSnapshot:
    payload: dict
    form: OptimizationForm
    result: Result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def sign_snapshot(payload):
    return signing.dumps(payload, salt=SALT, compress=True)


def _hours(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def _metrics(metrics):
    return {name: str(getattr(metrics, name)) for name in METRICS}


def issue_snapshot(form, result, actor):
    if (result.status.value not in ALLOWED or result.assignment is None or result.metrics is None):
        return None
    checked = evaluate_assignment(form.to_problem(), result.assignment)
    if checked != result.metrics:
        raise RuntimeError("M2 result disagrees with direct mathematical validation")
    data = form.cleaned_data
    source = {"request_count": str(form.request_count), "employee_count": str(form.employee_count),
              "alpha": format(data["alpha"], "f"), "beta": format(data["beta"], "f")}
    users = {}
    for j in range(form.employee_count):
        identifier = data[f"employee_{j}"]
        users[str(identifier)] = form.users[identifier].get_username()
        source.update({f"employee_{j}": str(identifier), f"capacity_{j}": _hours(data[f"capacity_{j}"]),
                       f"occupied_{j}": _hours(data[f"occupied_{j}"]), f"rate_{j}": format(data[f"rate_{j}"], "f")})
    for i in range(form.request_count):
        source.update({f"request_id_{i}": str(int(data[f"request_id_{i}"])), f"title_{i}": data[f"title_{i}"]})
        for j in range(form.employee_count):
            value = data[f"effort_{i}_{j}"]
            source[f"effort_{i}_{j}"] = "" if value is None else _hours(value)
    return sign_snapshot({"version": 1, "stage": "calculation", "token": str(uuid.uuid4()),
        "actor": actor.pk, "issued": int(time.time()), "source": source, "users": users,
        "plan": {"status": result.status.value, "assignment": [list(pair) for pair in result.assignment],
                 "metrics": _metrics(checked), "nodes": result.nodes_visited, "reason": result.termination_reason}})


def _details_shape(payload):
    details = payload.get("details")
    if not isinstance(details, dict) or set(details) != {"horizon_start", "due_at", "estimates_confirmed", "rows"}:
        raise CommitError("Состав предпросмотра повреждён.", 400)
    if (details["estimates_confirmed"] is not True or not isinstance(details["rows"], list)
            or not 1 <= len(details["rows"]) <= 6):
        raise CommitError("Неполный пакет подтверждения.", 400)
    for name in ("horizon_start", "due_at"):
        if not isinstance(details[name], str) or len(details[name]) > 32:
            raise CommitError("Некорректный плановый горизонт.", 400)
    ids = []
    for row in details["rows"]:
        if (not isinstance(row, dict) or set(row) != {"local_id", "client", "client_digest", "description"}
                or type(row["local_id"]) is not int or not 1 <= row["local_id"] <= 999999
                or type(row["client"]) is not int or not 1 <= row["client"] < 2**63
                or not isinstance(row["client_digest"], str) or len(row["client_digest"]) != 64
                or not isinstance(row["description"], str) or len(row["description"]) > 1000):
            raise CommitError("Некорректная строка предпросмотра.", 400)
        ids.append(row["local_id"])
    if len(set(ids)) != len(ids):
        raise CommitError("Повторяющиеся строки пакета.", 400)


def decode_snapshot(raw, actor, *, stage, allow_expired=False):
    if not isinstance(raw, str) or not 1 <= len(raw) <= OPTIMIZATION_SNAPSHOT_LIMIT:
        raise CommitError("Некорректный размер снимка.", 400)
    try:
        payload = signing.loads(raw, salt=SALT, max_age=None if allow_expired else SNAPSHOT_TTL)
    except signing.SignatureExpired as error:
        raise CommitError("Срок снимка истёк. Выполните новый расчёт.", 410) from error
    except (signing.BadSignature, ValueError, TypeError) as error:
        raise CommitError("Подпись снимка недействительна. Выполните новый расчёт.", 400) from error
    keys = {"version", "stage", "token", "actor", "issued", "source", "users", "plan"}
    if stage == "prepared":
        keys.add("details")
    if (not isinstance(payload, dict) or set(payload) != keys or type(payload["version"]) is not int
            or payload["version"] != 1 or payload["stage"] != stage):
        raise CommitError("Неподдерживаемый снимок подтверждения.", 400)
    if type(payload["actor"]) is not int or payload["actor"] != actor.pk:
        raise CommitError("Этот снимок принадлежит другому администратору.", 403)
    try:
        if str(uuid.UUID(payload["token"])) != payload["token"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise CommitError("Некорректный идентификатор подтверждения.", 400) from error
    if type(payload["issued"]) is not int or payload["issued"] > time.time() + 5:
        raise CommitError("Некорректное время снимка.", 400)
    if not allow_expired and time.time() - payload["issued"] > SNAPSHOT_TTL:
        raise CommitError("Срок исходного расчёта истёк. Выполните новый расчёт.", 410)
    if not isinstance(payload["plan"], dict) or payload["plan"].get("status") not in ALLOWED:
        raise CommitError("Нет полного допустимого плана B&B.", 400)
    if stage == "prepared":
        _details_shape(payload)
    return payload


def read_snapshot(raw, actor, *, stage="calculation"):
    payload = decode_snapshot(raw, actor, stage=stage)
    source = payload["source"]
    if not isinstance(source, dict) or not all(isinstance(v, str) for v in source.values()):
        raise CommitError("Некорректные экспертные входы.", 400)
    sizes = OptimizationSizeForm(source)
    if not sizes.is_valid():
        raise CommitError("Размер пакета не соответствует пределам М3А.", 400)
    users = list(get_user_model().objects.filter(is_active=True).order_by("username", "pk"))
    form = OptimizationForm(source, users=users, **sizes.cleaned_data)
    if set(source) != set(form.fields) | {"request_count", "employee_count"} or not form.is_valid():
        raise CommitError("Исходные данные или активность сотрудников изменились. Выполните новый расчёт.")
    actual_names = {str(form.cleaned_data[f"employee_{j}"]): form.users[form.cleaned_data[f"employee_{j}"]].get_username()
                    for j in range(form.employee_count)}
    if actual_names != payload["users"]:
        raise CommitError("Данные сотрудников изменились. Выполните новый расчёт.")
    plan = payload["plan"]
    if (not isinstance(plan, dict) or set(plan) != {"status", "assignment", "metrics", "nodes", "reason"}
            or plan["status"] not in ALLOWED or type(plan["nodes"]) is not int
            or not 0 <= plan["nodes"] <= 10000 or not isinstance(plan["reason"], str) or len(plan["reason"]) > 100):
        raise CommitError("Нет полного допустимого плана B&B.", 400)
    try:
        metrics = evaluate_assignment(form.to_problem(), plan["assignment"])
    except InputError as error:
        raise CommitError("План не соответствует квалификации и мощности исходного расчёта.", 400) from error
    if _metrics(metrics) != plan["metrics"]:
        raise CommitError("Точные метрики плана не совпали с исходными.", 400)
    assignment = tuple(sorted(tuple(pair) for pair in plan["assignment"]))
    if stage == "prepared" and {row["local_id"] for row in payload["details"]["rows"]} != {i for i, _ in assignment}:
        raise CommitError("Предпросмотр содержит неполный пакет.", 400)
    result = Result(Status(plan["status"]), assignment, metrics, plan["nodes"], plan["reason"],
                    optimality_proven=plan["status"] == Status.OPTIMAL.value)
    return VerifiedSnapshot(payload, form, result)


def client_digest(client):
    return digest({field.name: (getattr(client, field.name).isoformat() if hasattr(getattr(client, field.name), "isoformat")
                               else getattr(client, field.name)) for field in Client._meta.concrete_fields})


def request_form(verified, row, due_at, actor):
    source = verified.form.cleaned_data
    titles = {int(source[f"request_id_{i}"]): source[f"title_{i}"] for i in range(verified.form.request_count)}
    return ClientRequestForm({"client": row["client"], "title": titles[row["local_id"]],
        "description": row["description"], "assigned_to": dict(verified.result.assignment)[row["local_id"]],
        "due_at": due_at}, user=actor)


def prepare_snapshot(verified, details_form, actor):
    if not details_form.is_valid():
        raise ValueError("Expected valid package details")
    data = details_form.cleaned_data
    rows = []
    for identifier, _ in verified.result.assignment:
        client = data[f"client_{identifier}"]
        row = {"local_id": identifier, "client": client.pk, "client_digest": client_digest(client),
               "description": data[f"description_{identifier}"]}
        if not request_form(verified, row, data["due_at"], actor).is_valid():
            raise CommitError(f"Заявка {identifier} не прошла действующую форму CRM.")
        rows.append(row)
    payload = {**verified.payload, "stage": "prepared", "details": {
        "horizon_start": data["horizon_start"].isoformat(), "due_at": data["due_at"].isoformat(),
        "estimates_confirmed": True, "rows": rows}}
    return sign_snapshot(payload)


def _existing(payload, actor):
    record = OptimizationCommit.objects.filter(token=payload["token"]).first()
    if record is not None:
        if record.created_by_id != actor.pk or record.digest != digest(payload):
            raise CommitError("Этот токен уже использован с другим содержимым или автором.")
        if not record.created_ids:
            raise CommitError("Результат подтверждения недоступен.", 503)
    return record


def commit_snapshot(raw, actor, *, accept_unproven=False):
    """INSERT claims the unique token before business writes; rollback includes the receipt."""
    try:
        payload = decode_snapshot(raw, actor, stage="prepared", allow_expired=True)
        current = get_user_model().objects.filter(pk=actor.pk).first()
        if current is None or not current.is_active or not current.is_superuser:
            raise CommitError("Право подтверждения больше недоступно.", 403)
        if payload["plan"].get("status") == Status.FEASIBLE_NOT_PROVEN.value and accept_unproven is not True:
            raise CommitError("Необходимо явно принять недоказанное решение.", 400)
        existing = _existing(payload, actor)
        if existing is not None:
            return existing
        verified = read_snapshot(raw, actor, stage="prepared")
        with transaction.atomic():
            record = OptimizationCommit.objects.create(token=uuid.UUID(payload["token"]), created_by=current, digest=digest(payload))
            # SQLite's first INSERT takes the write lock. select_for_update is useful
            # on supporting backends, but is not claimed to supply row locks on SQLite.
            user_ids = {employee.id for employee in verified.form.to_problem().employees} | {actor.pk}
            locked = {user.pk: user for user in get_user_model().objects.select_for_update().filter(pk__in=user_ids).order_by("pk")}
            if actor.pk not in locked or not locked[actor.pk].is_active or not locked[actor.pk].is_superuser:
                raise CommitError("Права администратора изменились.", 403)
            verified = read_snapshot(raw, locked[actor.pk], stage="prepared")
            details = payload["details"]
            client_ids = {row["client"] for row in details["rows"]}
            clients = {c.pk: c for c in Client.objects.select_for_update().filter(pk__in=client_ids).order_by("pk")}
            for row in details["rows"]:
                if row["client"] not in clients or client_digest(clients[row["client"]]) != row["client_digest"]:
                    raise CommitError("Клиент удалён или изменён. Подготовьте новый предпросмотр.")
            data = {"snapshot": raw, "horizon_start": details["horizon_start"], "due_at": details["due_at"], "estimates_confirmed": "on"}
            for row in details["rows"]:
                data.update({f"client_{row['local_id']}": str(row["client"]), f"description_{row['local_id']}": row["description"]})
            details_form = OptimizationDetailsForm(data, local_ids=[i for i, _ in verified.result.assignment])
            if not details_form.is_valid():
                raise CommitError("Клиенты или общий срок больше не подходят. Подготовьте новый предпросмотр.")
            due_at = details_form.cleaned_data["due_at"]
            created = []
            for row in details["rows"]:
                form = request_form(verified, row, due_at, locked[actor.pk])
                if not form.is_valid():
                    raise CommitError(f"Заявка {row['local_id']} не прошла повторную проверку CRM.")
                linked_request = create_request_with_task(form)
                task = Task.objects.get(request=linked_request, is_automatic=True)
                owner = dict(verified.result.assignment)[row["local_id"]]
                if (linked_request.status != "new" or task.status != "new" or linked_request.assigned_to_id != owner
                        or task.assigned_to_id != owner or linked_request.due_at != due_at or task.due_at != due_at):
                    raise RuntimeError("Created CRM records violate the package contract")
                created.append({"local_id": row["local_id"], "request_id": linked_request.pk,
                                "task_id": task.pk, "employee_id": owner})
            if due_at <= timezone.now() or time.time() - payload["issued"] > SNAPSHOT_TTL:
                raise CommitError("Срок подтверждения или обработки истёк. Выполните новый расчёт.", 410)
            record.created_ids = created
            record.save(update_fields=["created_ids"])
            return record
    except IntegrityError:
        try:
            existing = _existing(payload, actor)
        except OperationalError as error:
            raise CommitError("База данных занята. Повторите это же подтверждение.", 503, retry=True) from error
        if existing is not None:
            return existing
        raise
    except OperationalError as error:
        raise CommitError("Не удалось обратиться к базе данных. Повторите это же подтверждение: токен защищает от дублей.", 503, retry=True) from error
