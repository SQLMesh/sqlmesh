from __future__ import annotations

import re
import typing as t

from pydantic.functional_validators import BeforeValidator

from sqlmesh.core.config.base import BaseConfig
from sqlmesh.core.config.common import compile_regex_mapping

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter.base import EngineAdapter
    OwnershipMapping = t.Dict[re.Pattern, str]
    EnvironmentOwnerResolver = t.Callable[[str, EngineAdapter], t.Optional[str]]
    PhysicalOwnerResolver = t.Callable[[EngineAdapter], t.Optional[str]]
else:
    OwnershipMapping = t.Annotated[t.Dict[re.Pattern, str], BeforeValidator(compile_regex_mapping)]
    EnvironmentOwnerResolver = t.Callable
    PhysicalOwnerResolver = t.Callable


class OwnershipConfig(BaseConfig):
    """Configuration for object ownership rules applied at creation time.

    For static YAML-based config, use ``environment_owner_mapping`` and
    ``physical_owner``.  For programmatic config where the principal must be
    resolved at plan-execution time (e.g. via ``adapter.current_user()`` or a
    Databricks API call), supply ``environment_owner_resolver`` and/or
    ``physical_owner_resolver`` instead — callables take precedence over the
    static fields.

    Example (YAML)::

        ownership:
          environment_owner_mapping:
            "^prod$": "svc_prod_spn"
            ".*": "group:shared-developers"
          physical_owner: "group:shared-developers"

    Example (Python)::

        OwnershipConfig(
            environment_owner_resolver=lambda env, adapter: (
                adapter.current_user() if env == "prod" else "group:shared-developers"
            ),
            physical_owner="group:shared-developers",
        )
    """

    environment_owner_mapping: OwnershipMapping = {}
    environment_owner_resolver: t.Optional[EnvironmentOwnerResolver] = None
    physical_owner: t.Optional[str] = None
    physical_owner_resolver: t.Optional[PhysicalOwnerResolver] = None

    @property
    def is_active(self) -> bool:
        """True when any ownership rule is configured."""
        return bool(
            self.environment_owner_resolver is not None
            or self.environment_owner_mapping
            or self.physical_owner is not None
            or self.physical_owner_resolver is not None
        )

    def resolve_owner(self, environment_name: str, adapter: "EngineAdapter") -> t.Optional[str]:
        """Return the configured owner for the given environment, or None."""
        if self.environment_owner_resolver is not None:
            return self.environment_owner_resolver(environment_name, adapter)
        for pattern, owner in self.environment_owner_mapping.items():
            if pattern.fullmatch(environment_name):
                return owner
        return None

    def resolve_physical_owner(self, adapter: "EngineAdapter") -> t.Optional[str]:
        """Return the configured physical-layer owner, or None."""
        if self.physical_owner_resolver is not None:
            return self.physical_owner_resolver(adapter)
        return self.physical_owner
