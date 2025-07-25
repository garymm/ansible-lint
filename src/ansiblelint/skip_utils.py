# (c) 2019-2020, Ansible by Red Hat
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

"""Utils related to inline skipping of rules."""

from __future__ import annotations

import collections.abc
import logging
import re
import warnings
from collections.abc import Mapping, MutableMapping, Sequence
from functools import cache
from itertools import product
from typing import TYPE_CHECKING, Any

# Module 'ruamel.yaml' does not explicitly export attribute 'YAML'; implicit reexport disabled
from ruamel.yaml import YAML
from ruamel.yaml.composer import ComposerError
from ruamel.yaml.scanner import ScannerError
from ruamel.yaml.tokens import CommentToken

from ansiblelint.config import used_old_tags
from ansiblelint.constants import (
    NESTED_TASK_KEYS,
    PLAYBOOK_TASK_KEYWORDS,
    RENAMED_TAGS,
    SKIPPED_RULES_KEY,
)
from ansiblelint.errors import LintWarning, WarnSource

if TYPE_CHECKING:
    from collections.abc import Generator

    from ansiblelint.file_utils import Lintable
    from ansiblelint.types import AnsibleBaseYAMLObject


_logger = logging.getLogger(__name__)
_found_deprecated_tags: set[str] = set()
_noqa_comment_re = re.compile(r"^\s*# noqa(\s|:)", flags=re.MULTILINE)
_noqa_comment_line_re = re.compile(r"^\s*# noqa(\s|:).*$")

# playbook: Sequence currently expects only instances of one of the two
# classes below but we should consider avoiding this chimera.
# ruamel.yaml.comments.CommentedSeq
# ansible.parsing.yaml.objects.AnsibleSequence


def get_rule_skips_from_line(
    line: str,
    lintable: Lintable,
    lineno: int = 1,
) -> list[str]:
    """Return list of rule ids skipped via comment on the line of yaml."""
    _before_noqa, _noqa_marker, noqa_text = line.partition("# noqa")

    result = []
    for v in noqa_text.lstrip(" :").split():
        if v in RENAMED_TAGS:
            tag = RENAMED_TAGS[v]
            if v not in _found_deprecated_tags:
                msg = f"Replaced outdated tag '{v}' with '{tag}', replace it to avoid future errors"
                warnings.warn(
                    message=msg,
                    category=LintWarning,
                    source=WarnSource(
                        filename=lintable,
                        lineno=lineno,
                        tag="warning[outdated-tag]",
                        message=msg,
                    ),
                    stacklevel=0,
                )
                _found_deprecated_tags.add(v)
            v = tag
        result.append(v)
    return result


def append_skipped_rules(
    pyyaml_data: AnsibleBaseYAMLObject,
    lintable: Lintable,
) -> AnsibleBaseYAMLObject:
    """Append 'skipped_rules' to individual tasks or single metadata block.

    For a file, uses 2nd parser (ruamel.yaml) to pull comments out of
    yaml subsets, check for '# noqa' skipped rules, and append any skips to the
    original parser (pyyaml) data relied on by remainder of ansible-lint.

    :param pyyaml_data: file text parsed via ansible and pyyaml.
    :param file_text: raw file text.
    :param file_type: type of file: tasks, handlers or meta.
    :returns: original pyyaml_data altered with a 'skipped_rules' list added \
              to individual tasks, or added to the single metadata block.
    """
    try:
        yaml_skip = _append_skipped_rules(pyyaml_data, lintable)
    except RuntimeError:  # pragma: no cover
        # Notify user of skip error, do not stop, do not change exit code
        _logger.exception("Error trying to append skipped rules")
        return pyyaml_data

    if not yaml_skip:
        return pyyaml_data

    return yaml_skip


@cache
def load_data(file_text: str) -> Any:
    """Parse ``file_text`` as yaml and return parsed structure.

    This is the main culprit for slow performance, each rule asks for loading yaml again and again
    ideally the ``maxsize`` on the decorator above MUST be great or equal total number of rules
    :param file_text: raw text to parse
    :return: Parsed yaml
    """
    yaml = YAML()
    # Ruamel role is not to validate the yaml file, so we ignore duplicate keys:
    yaml.allow_duplicate_keys = True
    try:
        return yaml.load(file_text)
    except ComposerError:
        # load fails on multi-documents with ComposerError exception
        return yaml.load_all(file_text)


def _append_skipped_rules(
    pyyaml_data: AnsibleBaseYAMLObject,
    lintable: Lintable,
) -> AnsibleBaseYAMLObject | None:
    # parse file text using 2nd parser library
    try:
        ruamel_data = load_data(lintable.content)
    except ScannerError as exc:  # pragma: no cover
        _logger.debug(
            "Ignored loading skipped rules from file %s due to: %s",
            lintable,
            exc,
        )
        # For unparsable file types, we return empty skip lists
        return None
    skipped_rules = _get_rule_skips_from_yaml(ruamel_data, lintable)

    if lintable.kind in [
        "yaml",
        "requirements",
        "vars",
        "meta",
        "reno",
        "test-meta",
        "galaxy",
    ]:
        # AnsibleMapping, dict
        if isinstance(pyyaml_data, MutableMapping):
            pyyaml_data[SKIPPED_RULES_KEY] = skipped_rules
        # AnsibleSequence, list
        elif (
            not isinstance(pyyaml_data, str)
            and isinstance(pyyaml_data, collections.abc.Sequence)
            and skipped_rules
        ):
            pyyaml_data[0][SKIPPED_RULES_KEY] = skipped_rules

        return pyyaml_data

    # create list of blocks of tasks or nested tasks
    pyyaml_task_blocks: Sequence[Any]
    if lintable.kind in ("tasks", "handlers", "playbook"):
        if not isinstance(pyyaml_data, Sequence):
            return pyyaml_data
        if lintable.kind in ("tasks", "handlers"):
            ruamel_task_blocks = ruamel_data
            pyyaml_task_blocks = pyyaml_data
        else:
            try:
                pyyaml_task_blocks = _get_task_blocks_from_playbook(pyyaml_data)
                ruamel_task_blocks = _get_task_blocks_from_playbook(ruamel_data)
            except (AttributeError, TypeError):
                return pyyaml_data
    else:
        # For unsupported file types, we return empty skip lists
        return None

    # get tasks from blocks of tasks
    pyyaml_tasks = _get_tasks_from_blocks(pyyaml_task_blocks)
    ruamel_tasks = _get_tasks_from_blocks(ruamel_task_blocks)

    # append skipped_rules for each task
    for ruamel_task, pyyaml_task in zip(ruamel_tasks, pyyaml_tasks, strict=False):
        # ignore empty tasks
        if not pyyaml_task and not ruamel_task:
            continue

        # AnsibleUnicode or str
        if isinstance(pyyaml_task, str):
            continue

        if pyyaml_task.get("name") != ruamel_task.get("name"):  # pragma: no cover
            msg = "Error in matching skip comment to a task"
            raise RuntimeError(msg)
        pyyaml_task[SKIPPED_RULES_KEY] = _get_rule_skips_from_yaml(
            ruamel_task,
            lintable,
        )

    return pyyaml_data


def _get_task_blocks_from_playbook(playbook: Sequence[Any]) -> list[Any]:
    """Return parts of playbook that contains tasks, and nested tasks.

    :param playbook: playbook yaml from yaml parser.
    :returns: list of task dictionaries.
    """
    task_blocks = []
    for play, key in product(playbook, PLAYBOOK_TASK_KEYWORDS):
        task_blocks.extend(play.get(key, []))
    return task_blocks


def _get_tasks_from_blocks(task_blocks: Sequence[Any]) -> Generator[Any, None, None]:
    """Get list of tasks from list made of tasks and nested tasks."""
    if not task_blocks:
        return

    def get_nested_tasks(task: Any) -> Generator[Any, None, None]:
        if not task or not is_nested_task(task):
            return
        for k in NESTED_TASK_KEYS:
            if task.get(k):
                if hasattr(task[k], "get"):
                    continue
                for subtask in task[k]:
                    yield from get_nested_tasks(subtask)
                    yield subtask

    for task in task_blocks:
        yield from get_nested_tasks(task)
        yield task


def _continue_skip_next_lines(
    lintable: Lintable,
) -> None:
    """When a line only contains a noqa comment (and possibly indentation), add the skip also to the next non-empty line."""
    # If line starts with _noqa_comment_line_re, add next non-empty line to same lintable.line_skips
    line_content = lintable.content.splitlines()
    for line_no in list(lintable.line_skips.keys()):
        if _noqa_comment_line_re.fullmatch(line_content[line_no - 1]):
            # Find next non-empty line
            next_line_no = line_no
            while (
                next_line_no < len(line_content)
                and not line_content[next_line_no].strip()
            ):
                next_line_no += 1
            if next_line_no >= len(line_content):
                continue
            lintable.line_skips[next_line_no + 1].update(
                lintable.line_skips[line_no],
            )


def _get_rule_skips_from_yaml(
    yaml_input: Sequence[Any],
    lintable: Lintable,
) -> Sequence[Any]:
    """Traverse yaml for comments with rule skips and return list of rules."""
    yaml_comment_obj_strings = []

    if isinstance(yaml_input, str):
        return []

    def traverse_yaml(obj: Any) -> None:
        traversable = list(obj.ca.items.values())
        if obj.ca.comment:
            traversable.append(obj.ca.comment)
        for entry in traversable:
            # flatten all lists we might have in entries. Some arcane ruamel CommentedMap magic
            entry = [
                item
                for sublist in entry
                if sublist is not None
                for item in (sublist if isinstance(sublist, list) else [sublist])
            ]
            for v in entry:
                if isinstance(v, CommentToken):
                    comment_str = v.value
                    if _noqa_comment_re.match(comment_str):
                        line = v.start_mark.line + 1  # ruamel line numbers start at 0
                        lintable.line_skips[line].update(
                            get_rule_skips_from_line(
                                comment_str.strip(),
                                lintable=lintable,
                                lineno=line,
                            ),
                        )
        yaml_comment_obj_strings.append(str(obj.ca.items))
        if isinstance(obj, dict):
            for val in obj.values():
                if isinstance(val, dict | list):
                    traverse_yaml(val)
        elif isinstance(obj, list):
            for element in obj:
                if isinstance(element, dict | list):
                    traverse_yaml(element)

    if isinstance(yaml_input, dict | list):
        traverse_yaml(yaml_input)

    rule_id_list = []
    for comment_obj_str in yaml_comment_obj_strings:
        for line in comment_obj_str.split(r"\n"):
            rule_id_list.extend(get_rule_skips_from_line(line, lintable=lintable))
    _continue_skip_next_lines(lintable)

    return [normalize_tag(tag) for tag in rule_id_list]


def normalize_tag(tag: str) -> str:
    """Return current name of tag."""
    if tag in RENAMED_TAGS:  # pragma: no cover
        used_old_tags[tag] = RENAMED_TAGS[tag]
        return RENAMED_TAGS[tag]
    return tag


def is_nested_task(task: Mapping[str, Any]) -> bool:
    """Check if task includes block/always/rescue."""
    # Cannot really trust the input
    if isinstance(task, str):
        return False
    # https://github.com/ansible/ansible-lint/issues/4492
    if not hasattr(task, "get"):
        return False

    return any(task.get(key) for key in NESTED_TASK_KEYS)
