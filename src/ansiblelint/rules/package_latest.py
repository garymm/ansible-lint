"""Implementations of the package-latest rule."""

# Copyright (c) 2016 Will Thames <will@thames.id.au>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import annotations

from typing import TYPE_CHECKING

from ansiblelint.rules import AnsibleLintRule

if TYPE_CHECKING:
    from ansiblelint.file_utils import Lintable
    from ansiblelint.utils import Task


class PackageIsNotLatestRule(AnsibleLintRule):
    """Package installs should not use latest."""

    id = "package-latest"
    description = (
        "Package installs should use ``state=present`` with or without a version"
    )
    severity = "VERY_LOW"
    tags = ["idempotency"]
    version_changed = "6.20.0"

    _package_managers = [
        "apk",
        "apt",
        "bower",
        "bundler",
        "dnf",
        "easy_install",
        "gem",
        "homebrew",
        "jenkins_plugin",
        "npm",
        "openbsd_package",
        "openbsd_pkg",
        "package",
        "pacman",
        "pear",
        "pip",
        "pkg5",
        "pkgutil",
        "portage",
        "slackpkg",
        "sorcery",
        "swdepot",
        "win_chocolatey",
        "yarn",
        "yum",
        "zypper",
    ]

    def matchtask(
        self,
        task: Task,
        file: Lintable | None = None,
    ) -> bool | str:
        return (
            task["action"]["__ansible_module__"] in self._package_managers
            and not task["action"].get("version")
            and not task["action"].get("update_only")
            and not task["action"].get("only_upgrade")
            and not task["action"].get("download_only")
            and task["action"].get("state") == "latest"
        )
