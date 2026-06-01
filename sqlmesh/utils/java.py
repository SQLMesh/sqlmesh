from __future__ import annotations

import re
import subprocess
import typing as t

from sqlmesh.utils import ttl_cache


@ttl_cache()
def java_major_version() -> t.Optional[int]:
    """Return the major Java version, or None if it cannot be determined."""
    try:
        proc = subprocess.run(["java", "-version"], capture_output=True, text=True, check=False)
        output = proc.stderr or proc.stdout
        if match := re.search(r'version "(\d+)(?:\.(\d+))?', output):
            major = int(match.group(1))
            if major == 1 and match.group(2):
                return int(match.group(2))
            return major
    except Exception:
        pass
    return None


def is_spark_java_supported() -> bool:
    """Spark's bundled Hadoop cannot initialize on Java 24+."""
    major = java_major_version()
    if major is None:
        return True
    return major < 24


def spark_java_options(extra: str = "") -> str:
    """Return JVM options needed for Spark on newer JDK releases."""
    options: t.List[str] = []
    if java_major_version() == 23:
        options.append("-Djava.security.manager=allow")
    if extra:
        options.append(extra.strip())
    return " ".join(options)
