from __future__ import annotations

from typing import List, Optional

from .base import CIVariable, Project, ProviderError, User
from ..gitlab import (
    GitLabError,
    delete_project_variable as _delete_variable,
    get_project_by_path as _get_project,
    list_project_variables as _list_variables,
    list_projects as _list_projects,
    set_project_variable as _set_variable,
    whoami as _whoami,
)


class GitLabProvider:
    def __init__(self, host: str, token: str) -> None:
        self._host = host
        self._token = token

    def whoami(self) -> User:
        try:
            u = _whoami(self._host, self._token)
            return User(id=u.id, username=u.username, name=u.name)
        except GitLabError as e:
            raise ProviderError(str(e)) from e

    def list_projects(
        self,
        search: Optional[str] = None,
        owned: bool = False,
        membership: bool = True,
    ) -> List[Project]:
        try:
            items = _list_projects(
                self._host, self._token,
                search=search, owned=owned, membership=membership,
            )
            return [
                Project(
                    id=str(p.id),
                    path=p.path_with_namespace,
                    web_url=p.web_url,
                    default_branch=p.default_branch,
                )
                for p in items
            ]
        except GitLabError as e:
            raise ProviderError(str(e)) from e

    def get_project(self, path: str) -> Project:
        try:
            p = _get_project(self._host, self._token, path)
            return Project(
                id=str(p.id),
                path=p.path_with_namespace,
                web_url=p.web_url,
                default_branch=p.default_branch,
            )
        except GitLabError as e:
            raise ProviderError(str(e)) from e

    def list_variables(self, project_id: str) -> List[CIVariable]:
        try:
            items = _list_variables(self._host, self._token, int(project_id))
            return [
                CIVariable(
                    key=v.key,
                    value=v.value,
                    protected=v.protected,
                    masked=v.masked,
                    environment_scope=v.environment_scope,
                )
                for v in items
            ]
        except GitLabError as e:
            raise ProviderError(str(e)) from e

    def set_variable(
        self,
        project_id: str,
        key: str,
        value: str,
        protected: bool = False,
        masked: bool = False,
        environment_scope: str = "*",
        variable_type: str = "env_var",
    ) -> None:
        try:
            _set_variable(
                host=self._host, token=self._token,
                project_id=int(project_id),
                key=key, value=value,
                protected=protected, masked=masked,
                environment_scope=environment_scope,
                variable_type=variable_type,
            )
        except GitLabError as e:
            raise ProviderError(str(e)) from e

    def delete_variable(
        self,
        project_id: str,
        key: str,
        environment_scope: str = "*",
    ) -> None:
        try:
            _delete_variable(
                host=self._host, token=self._token,
                project_id=int(project_id),
                key=key, environment_scope=environment_scope,
            )
        except GitLabError as e:
            raise ProviderError(str(e)) from e
