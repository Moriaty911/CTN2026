"""Local catalog registry with separately persisted platform account bindings."""
from dataclasses import replace
from typing import Iterable
from .models import Persona, PersonaArchetype
from .catalog import load_catalog, persona_from_record, digest


class PersonaRegistry:
    def __init__(self, personas: Iterable[Persona] | None = None, *, path=None):
        self.store = None
        self.catalog = load_catalog(path) if personas is None else None
        self.catalog_hash = digest(self.catalog) if self.catalog is not None else ''
        self._personas = {}
        for p in ([persona_from_record(r) for r in self.catalog['personas']] if self.catalog else personas or ()):
            self.register(p)

    def register(self, persona):
        if persona.persona_id in self._personas:
            raise ValueError('duplicate persona ID')
        self._personas[persona.persona_id] = persona

    def attach(self, store):
        self.store = store
        if self.catalog is not None:
            store.register_persona_catalog(self.catalog)
        return self

    def list_all(self):
        bindings = self.store.persona_bindings() if self.store and self.catalog else {}
        return [replace(p, account_id=bindings.get(p.persona_id,'')) if self.catalog else p
                for p in self._personas.values()]

    def get(self, identifier):
        return next((p for p in self.list_all() if identifier and identifier in (p.persona_id,p.account_id)),None)

    def list_by_side(self, side):
        return [p for p in self.list_all() if p.assigned_side == side.lower()]

    def list_by_group(self, group, side=None):
        return [p for p in self.list_all() if p.occupation_group == group and (side is None or p.assigned_side == side)]

    def list_by_archetype(self, archetype, side=None):
        # Explicit historical fixture compatibility; production routing uses groups.
        return [p for p in self.list_all() if p.archetype == archetype and (side is None or p.assigned_side == side)]


def get_default_registry():
    return PersonaRegistry()


def get_default_personas(side='yes'):
    return get_default_registry().list_by_side(side)


DEFAULT_PERSONAS = tuple(get_default_registry().list_all())
