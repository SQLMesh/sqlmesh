from __future__ import annotations

import re
import typing as t

from pydantic.functional_validators import BeforeValidator

from sqlmesh.core.config.base import BaseConfig
from sqlmesh.core.config.common import compile_regex_mapping

if t.TYPE_CHECKING:
    OwnershipMapping = t.Dict[re.Pattern, str]
else:
    OwnershipMapping = t.Annotated[
        t.Dict[re.Pattern, str], BeforeValidator(compile_regex_mapping)
    ]


class OwnershipConfig(BaseConfig):
    """Configuration for object ownership rules applied at creation time.

    Maps environment name regex patterns to owner principals. The first
    matching pattern wins. Ownership is applied immediately when schemas and
    views are created, so even a partially-completed run leaves objects in a
    manageable state.

    Example::

        ownership:
          environment_owner_mapping:
            "^prod$": "svc_prod_spn"
            ".*": "group:shared-developers"
          physical_owner: "group:shared-developers"
    """

    environment_owner_mapping: OwnershipMapping = {}
    physical_owner: t.Optional[str] = None

    def resolve_owner(self, environment_name: str) -> t.Optional[str]:
        """Return the configured owner for the given environment name, or None."""
        for pattern, owner in self.environment_owner_mapping.items():
            if pattern.fullmatch(environment_name):
                return owner
        return None
