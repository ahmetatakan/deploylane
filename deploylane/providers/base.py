from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable


@dataclass
class User:
    id: int
    username: str
    name: str


@dataclass
class Project:
    id: str
    path: str
    web_url: Optional[str] = None
    default_branch: Optional[str] = None


@dataclass
class CIVariable:
    key: str
    value: Optional[str]
    protected: bool
    masked: bool
    environment_scope: str


class ProviderError(RuntimeError):
    pass


@runtime_checkable
class Provider(Protocol):
    def whoami(self) -> User: ...

    def list_projects(
        self,
        search: Optional[str] = None,
        owned: bool = False,
        membership: bool = True,
    ) -> List[Project]: ...

    def get_project(self, path: str) -> Project: ...

    def list_variables(self, project_id: str) -> List[CIVariable]: ...

    def set_variable(
        self,
        project_id: str,
        key: str,
        value: str,
        protected: bool = False,
        masked: bool = False,
        environment_scope: str = "*",
        variable_type: str = "env_var",
    ) -> None: ...

    def delete_variable(
        self,
        project_id: str,
        key: str,
        environment_scope: str = "*",
    ) -> None: ...
