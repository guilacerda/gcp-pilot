from __future__ import annotations

import os
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Type, Generator, get_args, Dict, ClassVar, Any, Tuple, get_type_hints, Union, Callable, List

from google.cloud import datastore

from gcp_pilot import exceptions

DEFAULT_NAMESPACE = os.environ.get("GCP_DATASTORE_NAMESPACE", default=None)
DEFAULT_PK_FIELD = "id"
MAX_ITEMS_PER_OPERATIONS = 500  # Datastore cannot write more than 500 items per call


def _chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


@dataclass
class DoesNotExist(Exception):
    cls: Type[EmbeddedDocument]
    filters: Dict


@dataclass
class MultipleObjectsFound(Exception):
    cls: Type[EmbeddedDocument]
    filters: Dict


def _starts_with_operator(lookup_fields, value) -> List[Tuple[str, str, Any]]:
    field_name = ".".join(lookup_fields)
    return [
        (field_name, ">=", value),
        (field_name, "<=", f"{value}\ufffd"),
    ]


@dataclass
class Manager:
    lookup_operators: ClassVar[Dict[str, Union[str, Callable]]] = {
        "eq": "=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "in": "in",
        "startswith": _starts_with_operator,
    }

    _client: ClassVar[datastore.Client] = None
    fields: Dict[str, type]
    pk_field: str
    doc_klass: Type[Document]
    kind: str

    def get_client(self) -> datastore.Client:
        if not self._client:
            self._client = datastore.Client(namespace=self.get_namespace())
        return self._client

    def get_namespace(self):
        return self.doc_klass.Meta.namespace

    def build_key(self, pk: Any = None) -> datastore.Key:
        if pk:
            typed_pk = self.doc_klass.Meta.fields[self.pk_field](pk)
            return self.get_client().key(self.kind, typed_pk)
        # If no primary key is provided, we let the server create a new ID
        return self.get_client().allocate_ids(self.get_client().key(self.kind), 1)[0]

    def _iterate(self, query, page_size):
        cursor = None
        empty = False

        while not empty:
            query_iter = query.fetch(start_cursor=cursor, limit=page_size)

            page = next(query_iter.pages, [])
            for item in page:
                yield item
            cursor = query_iter.next_page_token
            empty = not bool(cursor)

    def query(
        self,
        distinct_on: str = None,
        order_by: Union[str, List[str]] = None,
        page_size: int = None,
        **kwargs,
    ) -> datastore.query.Iterator:
        # base query
        query = self.get_client().query(kind=self.kind)
        if order_by:
            query.order = order_by
        if distinct_on:
            query.distinct_on = distinct_on

        # parse lookup args
        cross_filters = []
        for key, value in kwargs.items():
            all_filters = self._build_filter(key=key, value=value)
            for field_name, operator, field_value in all_filters:
                if operator == "in":
                    cross_filters.append((field_name, operator, field_value))
                else:
                    query.add_filter(field_name, operator, field_value)

        if not cross_filters:
            yield from self._iterate(query=query, page_size=page_size)
        else:
            # prepare combinations
            options = defaultdict(list)
            for name, operator, values in cross_filters:
                for value in values:
                    options[name].append((name, "=", value))

            combinations = itertools.product(*list(options.values()))

            found = set()
            for combination in combinations:
                current_query = query
                for field_name, operator, value in combination:
                    current_query = current_query.add_filter(field_name, operator, value)

                for item in self._iterate(query=current_query, page_size=page_size):
                    if item.id not in found:
                        yield item
                        found.add(item.id)

    def filter(self, **kwargs) -> Generator[Document, None, None]:
        for entity in self.query(**kwargs):
            yield self.from_entity(entity=entity)

    def get(self, **kwargs) -> Document:
        if self.pk_field in kwargs:
            pk = kwargs[self.pk_field]
            entity = self.get_client().get(key=self.build_key(pk=pk))
            if entity:
                return self.from_entity(entity=entity)
            raise DoesNotExist(self.doc_klass, pk)

        # Since we can't fetch directly from the key,
        # we filter and hope for just one object
        one_obj = None
        for obj in self.filter(**kwargs):
            if one_obj is not None:
                raise MultipleObjectsFound(self.doc_klass, filters=kwargs)
            one_obj = obj
        if not one_obj:
            raise DoesNotExist(self.doc_klass, filters=kwargs)
        return one_obj

    def create(self, obj: Document) -> Document:
        entity = self.to_entity(obj=obj)
        self.get_client().put(entity=entity)

        # if successfully saved, we assure the auto-generated ID is added to the final object
        if not obj.pk:
            setattr(obj, obj.Meta.pk_field, entity.id)
        return obj

    def update(self, pk: str, **kwargs) -> Document:
        if kwargs:
            entity = self.get_client().get(key=self.build_key(pk=pk))
            # TODO: enable partial nested updates
            as_data = {
                key: value.Meta.to_dict(obj=value) if isinstance(value, EmbeddedDocument) else value
                for key, value in kwargs.items()
            }
            entity.update(as_data)
            self.get_client().put(entity=entity)
        return self.get(id=pk)

    def delete(self, pk: str = None):
        if pk:
            self.get_client().delete(key=self.build_key(pk=pk))
        else:
            keys = [entity.key for entity in self.query()]
            for chunk in _chunks(keys, MAX_ITEMS_PER_OPERATIONS):
                self.get_client().delete_multi(keys=chunk)

    def _build_filter(self, key: str, value: Any) -> List[Tuple[str, str, Any]]:
        operator = None

        field_name, *extra = key.split("__")
        if extra:
            if len(extra) > 1:
                raise exceptions.UnsupportedFormatException(f"Unsupported lookup key format {extra}")
            operator = extra[0]
            if operator not in self.lookup_operators:
                raise exceptions.UnsupportedFormatException(f"Unsupported lookup {operator}")
            if callable(operator):
                return operator(field_name, value)

        parts = field_name.split(".")
        if len(parts) > 1 and parts[0] not in self.fields:
            raise exceptions.ValidationError(
                f"{parts[0]} is not a valid field. Excepted one of {' | '.join(self.fields)}"
            )

        return [(field_name, (operator or "="), value)]

    def to_entity(self, obj: Document) -> datastore.Entity:
        entity = datastore.Entity(key=self.build_key(pk=obj.pk))
        if not obj.pk:
            setattr(obj, obj.Meta.pk_field, entity.id)
        entity.update(obj.Meta.to_dict(obj=obj))
        return entity

    def from_entity(self, entity: datastore.Entity) -> Document:
        data = dict(entity.items())
        if self.pk_field not in data:
            data[self.pk_field] = entity.id
        return self.doc_klass.Meta.from_dict(data=data)


@dataclass
class Metadata:
    fields: Dict[str, type]
    doc_klass: Type[EmbeddedDocument]
    pk_field: str = None
    namespace: str = DEFAULT_NAMESPACE

    def from_dict(self, data: Dict) -> EmbeddedDocument:
        data = data.copy()

        def _build(klass: Union[EmbeddedDocument, Callable], value: Any):
            if value is None or klass == Any:  # pylint: disable=comparison-with-callable
                return value

            if issubclass(klass, EmbeddedDocument):
                return klass.Meta.from_dict(data=value)

            if klass == datetime:
                return klass.fromisoformat(str(value))

            return klass(value)

        parsed_data = {}
        for field_name, field_klass in self.fields.items():
            try:
                raw_value = data[field_name]
            except KeyError:
                continue

            if getattr(field_klass, "_name", "") == "List":
                inner_klass = get_args(field_klass)[0]  # TODO: test composite types
                item = [_build(klass=inner_klass, value=i) for i in raw_value]
            elif getattr(field_klass, "_name", "") == "Dict":
                inner_klass_key, inner_klass_value = get_args(field_klass)
                item = {
                    _build(klass=inner_klass_key, value=k): _build(klass=inner_klass_value, value=v)
                    for k, v in raw_value.items()
                }
            else:
                item = _build(klass=field_klass, value=raw_value)
            parsed_data[field_name] = item

        return self.doc_klass(**parsed_data)

    def to_dict(self, obj: EmbeddedDocument, select_fields: List[str] = None) -> dict:
        # TODO handle custom dynamic fields
        def _unbuild(value):
            if value is None:
                return value
            if isinstance(value, EmbeddedDocument):
                return value.Meta.to_dict(obj=value)
            if isinstance(value, Enum):
                return value.value
            return value

        data = {}
        for field_name, field_klass in self.fields.items():
            if select_fields and field not in select_fields:
                continue

            raw_value = getattr(obj, field_name)

            if getattr(field_klass, "_name", "") == "List":
                item = [_unbuild(value=i) for i in raw_value]
            elif getattr(field_klass, "_name", "") == "Dict":
                item = {_unbuild(value=k): _unbuild(value=v) for k, v in raw_value.items()}
            else:
                item = _unbuild(value=raw_value)

            data[field_name] = item

        return data


class ORM(type):
    def __new__(mcs, name, bases, attrs):
        is_abstract_model = mcs._is_abstract(name=name)
        is_concrete_model = mcs._is_concrete(bases=bases)

        if not is_abstract_model:
            # Since it was not explicitly provided, add id: str = None
            if not mcs._has_explicit_pk_field(attrs=attrs, bases=bases) and is_concrete_model:
                attrs["__annotations__"][DEFAULT_PK_FIELD] = int
                attrs[DEFAULT_PK_FIELD] = None

        new_cls = super().__new__(mcs, name, bases, attrs)

        if is_abstract_model:
            return new_cls

        # Metadata initialization
        typed_fields = mcs._extract_fields(klass=new_cls)
        new_cls.Meta = Metadata(
            fields=typed_fields,
            doc_klass=new_cls,
            namespace=getattr(new_cls, "__namespace__", None),
        )

        # Manager initialization
        if is_concrete_model:
            new_cls.documents = Manager(
                fields=typed_fields,
                pk_field=DEFAULT_PK_FIELD,
                doc_klass=new_cls,
                kind=name,
            )

            new_cls.Meta.pk_field = DEFAULT_PK_FIELD

        return new_cls

    @classmethod
    def _is_abstract(mcs, name: str) -> bool:
        return name in ["Document", "EmbeddedDocument"]

    @classmethod
    def _is_concrete(mcs, bases: Tuple[type]) -> bool:
        return "Document" in [base.__name__ for base in bases]

    @classmethod
    def _has_explicit_pk_field(mcs, attrs: Dict, bases: Tuple[type]) -> bool:
        if DEFAULT_PK_FIELD in attrs.get("__annotations__", []):
            return True

        for base in bases:
            if DEFAULT_PK_FIELD in get_type_hints(base):
                return True

        return False

    @classmethod
    def _extract_fields(mcs, klass: type) -> Dict[str, type]:
        def _ignore(var_type: str, k: type):
            is_private = var_type.startswith("_")
            is_class_var = getattr(k, "__origin__", None) == ClassVar  # pylint: disable=comparison-with-callable
            return is_private or is_class_var

        hints = get_type_hints(klass)
        typed_fields = {var_type: k for var_type, k in hints.items() if not _ignore(var_type, k)}
        return typed_fields


@dataclass
class EmbeddedDocument(metaclass=ORM):
    Meta: ClassVar[Metadata]

    @classmethod
    def deserialize(cls, **kwargs) -> EmbeddedDocument:
        return cls.Meta.from_dict(data=kwargs)

    def serialize(self) -> Dict:
        return self.Meta.to_dict(obj=self)


@dataclass
class Document(EmbeddedDocument):
    documents: ClassVar[Manager]

    @property
    def pk(self):
        return getattr(self, self.Meta.pk_field, None)

    def save(self) -> Document:
        return self.documents.create(obj=self)

    def delete(self) -> None:
        self.documents.delete(pk=self.id)


__all__ = (
    "DoesNotExist",
    "MultipleObjectsFound",
    "EmbeddedDocument",
    "Document",
)
